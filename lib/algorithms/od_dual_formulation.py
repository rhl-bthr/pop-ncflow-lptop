import os
import pickle
import re
from collections import defaultdict

import numpy as np
import math
import json
from gurobipy import GRB, Model, quicksum

from ..config import TOPOLOGIES_DIR
from ..constants import NUM_CORES
from ..graph_utils import path_to_edge_list
from ..lp_solver import LpSolver
from ..path_utils import find_paths, graph_copy_with_edge_weights, remove_cycles
from .abstract_formulation import AbstractFormulation, Objective

PATHS_DIR = os.path.join(TOPOLOGIES_DIR, "paths", "path-form")


class ODDualFormulation(AbstractFormulation):
    @classmethod
    def new_total_flow(
        cls, num_paths, edge_disjoint=True, dist_metric="inv-cap", out=None
    ):
        return cls(
            objective=Objective.TOTAL_FLOW,
            num_paths=num_paths,
            edge_disjoint=edge_disjoint,
            dist_metric=dist_metric,
            DEBUG=False,
            VERBOSE=False,
            out=out,
        )

    @classmethod
    def new_max_concurrent_flow(
        cls, num_paths, edge_disjoint=True, dist_metric="inv-cap", out=None
    ):
        return cls(
            objective=Objective.MAX_CONCURRENT_FLOW,
            num_paths=num_paths,
            edge_disjoint=edge_disjoint,
            dist_metric=dist_metric,
            DEBUG=False,
            VERBOSE=False,
            out=out,
        )

    @classmethod
    def new_min_max_link_util(
        cls, num_paths, edge_disjoint=True, dist_metric="inv-cap", out=None
    ):
        return cls(
            objective=Objective.MIN_MAX_LINK_UTIL,
            num_paths=num_paths,
            edge_disjoint=edge_disjoint,
            dist_metric=dist_metric,
            DEBUG=False,
            VERBOSE=False,
            out=out,
        )

    @classmethod
    def compute_demand_scale_factor(
        cls, num_paths, edge_disjoint=True, dist_metric="inv-cap", out=None
    ):
        return cls(
            objective=Objective.COMPUTE_DEMAND_SCALE_FACTOR,
            num_paths=num_paths,
            edge_disjoint=edge_disjoint,
            dist_metric=dist_metric,
            DEBUG=False,
            VERBOSE=False,
            out=out,
        )

    @classmethod
    def get_pf_for_obj(cls, objective, num_paths, **kargs):
        if objective == Objective.TOTAL_FLOW:
            return cls.new_total_flow(num_paths, **kargs)
        elif objective == Objective.MAX_CONCURRENT_FLOW:
            return cls.new_max_concurrent_flow(num_paths, **kargs)
        elif objective == Objective.MIN_MAX_LINK_UTIL:
            return cls.new_min_max_link_util(num_paths, **kargs)
        elif objective == Objective.COMPUTE_DEMAND_SCALE_FACTOR:
            return cls.compute_demand_scale_factor(num_paths, **kargs)
        else:
            print('objective "{}" not found'.format(objective))

    def __init__(
        self,
        *,
        objective,
        num_paths,
        edge_disjoint=True,
        dist_metric="inv-cap",
        DEBUG=False,
        VERBOSE=False,
        out=None
    ):
        super().__init__(objective, DEBUG, VERBOSE, out)
        if dist_metric != "inv-cap" and dist_metric != "min-hop":
            raise Exception(
                'invalid distance metric: {}; only "inv-cap" and "min-hop" are valid choices'.format(
                    dist_metric
                )
            )
        self._num_paths = num_paths
        self.edge_disjoint = edge_disjoint
        self.dist_metric = dist_metric

    # flow caps = [((k1, ..., kn), f1), ...]
    def _construct_path_lp(self, G, edge_to_paths, num_total_paths, sat_flows=[]):
        self._print("Constructing Path LP")
        m = Model("max-flow: path formulation")

        # Create variables: one for each path
        path_vars = m.addVars(num_total_paths, vtype=GRB.CONTINUOUS, lb=0.0, name="f")

        # Set objective
        if (
            self._objective == Objective.MIN_MAX_LINK_UTIL
            or self._objective == Objective.COMPUTE_DEMAND_SCALE_FACTOR
        ):
            self._print("{} objective".format(self._objective))

            if self._objective == Objective.MIN_MAX_LINK_UTIL:
                max_link_util_var = m.addVar(
                    vtype=GRB.CONTINUOUS, lb=0.0, ub=1.0, name="z"
                )
            else:
                # max link util can exceed 1.0
                max_link_util_var = m.addVar(vtype=GRB.CONTINUOUS, lb=0.0, name="z")

            m.setObjective(max_link_util_var, GRB.MINIMIZE)
            # Add edge util constraints
            for u, v, c_e in G.edges.data("capacity"):
                if (u, v) in edge_to_paths:
                    paths = edge_to_paths[(u, v)]
                    constr_vars = [path_vars[p] for p in paths]
                    if c_e == 0.0:
                        m.addConstr(quicksum(constr_vars) <= 0.0)
                    else:
                        m.addConstr(quicksum(constr_vars) / c_e <= max_link_util_var)

            # Add demand equality constraints
            commod_id_to_path_inds = {}
            self._demand_constrs = []
            for k, d_k, path_ids in self.commodities:
                commod_id_to_path_inds[k] = path_ids
                self._demand_constrs.append(
                    m.addConstr(quicksum(path_vars[p] for p in path_ids) == d_k)
                )

        else:
            if self._objective == Objective.TOTAL_FLOW:
                self._print("TOTAL FLOW objective")
                obj = quicksum(path_vars)
            elif self._objective == Objective.MAX_CONCURRENT_FLOW:
                self._print("MAX CONCURRENT FLOW objective")
                self.alpha = m.addVar(vtype=GRB.CONTINUOUS, lb=0.0, ub=1.0, name="a")
                m.update()
                for k, d_k, path_ids in self.commodities:
                    m.addConstr(
                        quicksum(path_vars[p] for p in path_ids) / d_k >= self.alpha
                    )
                obj = self.alpha
            m.setObjective(obj, GRB.MAXIMIZE)

            # Add edge capacity constraints
            for u, v, c_e in G.edges.data("capacity"):
                if (u, v) in edge_to_paths:
                    paths = edge_to_paths[(u, v)]
                    constr_vars = [path_vars[p] for p in paths]
                    m.addConstr(quicksum(constr_vars) <= c_e)
            # Add demand constraints
            commod_id_to_path_inds = {}
            self._demand_constrs = []
            for k, d_k, path_ids in self.commodities:
                commod_id_to_path_inds[k] = path_ids
                self._demand_constrs.append(
                    m.addConstr(quicksum(path_vars[p] for p in path_ids) <= d_k)
                )

        # Flow cap constraints
        for fixed_commods, flow_value in sat_flows:
            constr_vars = [
                path_vars[p] for k in fixed_commods for p in commod_id_to_path_inds[k]
            ]
            m.addConstr(quicksum(constr_vars) >= 0.99 * flow_value)

        if self.DEBUG:
            m.write("pf_debug.lp")
        return LpSolver(m, None, self.DEBUG, self.VERBOSE, self.out)

    @staticmethod
    def paths_full_fname(problem, num_paths, edge_disjoint, dist_metric):
        return os.path.join(
            PATHS_DIR,
            "{}-{}-paths_edge-disjoint-{}_dist-metric-{}-dict.pkl".format(
                problem.name, num_paths, edge_disjoint, dist_metric
            ),
        )

    @staticmethod
    def compute_paths(problem, num_paths, edge_disjoint, dist_metric):
        paths_dict = {}
        G = graph_copy_with_edge_weights(problem.G, dist_metric)
        for s_k in G.nodes:
            for t_k in G.nodes:
                if s_k == t_k:
                    continue
                paths = find_paths(G, s_k, t_k, num_paths, edge_disjoint)
                paths_no_cycles = [remove_cycles(path) for path in paths]
                paths_dict[(s_k, t_k)] = paths_no_cycles
        return paths_dict

    @staticmethod
    def read_paths_from_disk_or_compute(problem, num_paths, edge_disjoint, dist_metric):
        paths_fname = ODDualFormulation.paths_full_fname(
            problem, num_paths, edge_disjoint, dist_metric
        )
        print("Loading paths from pickle file", paths_fname)

        try:
            with open(paths_fname, "rb") as f:
                paths_dict = pickle.load(f)
                for key, paths in paths_dict.items():
                    paths_no_cycles = [remove_cycles(path) for path in paths]
                    paths_dict[key] = paths_no_cycles
                print("paths_dict size:", len(paths_dict))
                return paths_dict
        except FileNotFoundError:
            print("Unable to find {}".format(paths_fname))
            paths_dict = ODDualFormulation.compute_paths(
                problem, num_paths, edge_disjoint, dist_metric
            )
            print("Saving paths to pickle file")
            with open(paths_fname, "wb") as w:
                pickle.dump(paths_dict, w)
            return paths_dict

    def get_paths(self, problem):
        if not hasattr(self, "_paths_dict"):
            self._paths_dict = ODDualFormulation.read_paths_from_disk_or_compute(
                problem, self._num_paths, self.edge_disjoint, self.dist_metric
            )
        return self._paths_dict

    ###############################
    # Override superclass methods #
    ###############################

    def solve(self, problem, num_threads=NUM_CORES):
        MAX_ROUNDS = 20000
        ETA = 1
        NORMALIZATION = 1000
        self._problem = problem
        self._solver = self._construct_lp([])

        all_paths = self.get_paths(problem)
        edge_to_path_ids = defaultdict(list)
        path_ids_to_edge = defaultdict(list)
        sites_to_path_ids = defaultdict(list)
        path_id_to_min_capacity = defaultdict(lambda: 10000000)
        edge_to_capacity = defaultdict(int)
        sites_to_demand = defaultdict(int)

        for u, v, c_e in self._problem.G.edges.data("capacity"):
            edge_to_capacity[(u, v)] = c_e / NORMALIZATION
                

        xr_old = defaultdict(int)
        xr_new = defaultdict(int)

        xr_bar_new = defaultdict(int)
        xr_bar_old = defaultdict(int)

        all_paths_dicty = {}
        path_ids = 0


        for k, (s_k, t_k, d_k) in self.commodity_list:
            sites_to_demand[(s_k, t_k)] = d_k / NORMALIZATION
            paths = all_paths[(s_k, t_k)]
            for path in paths:
                all_paths_dicty[path_ids] = path
                xr_old[path_ids] = d_k
                sites_to_path_ids[(s_k, t_k)].append(path_ids)
                for edge in path_to_edge_list(path):
                    path_id_to_min_capacity[path_ids] = min(path_id_to_min_capacity[path_ids], edge_to_capacity[edge])
                    edge_to_path_ids[edge].append(path_ids)
                    path_ids_to_edge[path_ids].append(edge)
                path_ids += 1

        mu_old = defaultdict(lambda: 100)
        nu_old = defaultdict(lambda: 10)
        beta_old = defaultdict(lambda: 10)

        mu_new = defaultdict(int)
        nu_new = defaultdict(int)
        beta_new = defaultdict(int)

        def has_matrix_converged(old_matrix, new_matrix):
            return True

        # xr_old[0] = 10000/3
        # xr_old[1] = 5000/3
        # xr_old[2] = 10000/3

        print("xr_init:", dict(xr_old), "mu_old:", dict(mu_old), "nu_old:", dict(nu_old), "beta_old:", dict(beta_old))

        round = 1
        while True:
            EPSILON = min(0.5/(min(round, 10)), 0.009)
            # EPSILON = (MAX_ROUNDS - round)/(4 * MAX_ROUNDS)

            for edge in edge_to_path_ids:
                sum_xr = 0
                for path_id in edge_to_path_ids[edge]:
                    sum_xr += xr_old[path_id]
                mu_new[edge] = max(mu_old[edge] + EPSILON * (sum_xr - (ETA * edge_to_capacity[edge])), 0)

            for site in sites_to_path_ids:
                sum_xr = 0
                for path_id in sites_to_path_ids[site]:
                    sum_xr += xr_old[path_id]

                nu_new[site] = max(nu_old[site] + EPSILON * (sites_to_demand[site] - sum_xr), 0)
            
            for path_id in all_paths_dicty:
                beta_new[path_id] = max(beta_old[path_id] - (EPSILON * xr_old[path_id]), 0)
            
            for path_id in all_paths_dicty:
                sum_mu = 0
                sum_nu = 0
                for edge in path_ids_to_edge[path_id]:
                    sum_mu += mu_new[edge]
                sum_nu = nu_new[(all_paths_dicty[path_id][0], all_paths_dicty[path_id][-1])]

                denominator = sum_mu - sum_nu #+ beta_new[path_id]
                # print("path_id:", path_id, "denominator:", denominator, "sum_mu:", sum_mu, "sum_nu:", sum_nu)
                max_allocation = path_id_to_min_capacity[path_id]
                if denominator <= 0:
                    xr_new[path_id] = max_allocation
                # elif denominator < 0:
                #     xr_new[path_id] = max_allocation
                else:
                    xr_new[path_id] = min(1/(denominator), max_allocation)
                    # xr_new[path_id] = min((1 - EPSILON) * xr_old[path_id] + EPSILON * (min(1/(denominator), max_allocation)), max_allocation)
                    if path_id not in xr_bar_old:
                        xr_bar_new[path_id] = (1/round) * xr_new[path_id] 
                    else:
                        xr_bar_new[path_id] = (1 - (1/round)) * xr_bar_old[path_id] + (1/round) * xr_new[path_id] 
            
            # print(round, ",", xr_bar_new[0], ",", xr_bar_new[1], ",", xr_bar_new[2], ",", xr_new[0], ",", xr_new[1], ",", xr_new[2], ",", mu_new[(0, 1)], ",", mu_new[(1, 2)], ",", nu_new[(0, 1)], ",", nu_new[(0, 2)], ",", nu_new[(1, 2)])
            print(round, ",", sum([math.log(xr_new[x] * NORMALIZATION) for x in range(len(xr_new))]), ",", sum([xr_new[x] * NORMALIZATION for x in range(len(xr_new))]), ",", len(xr_new) , ",", [xr_new[x] * NORMALIZATION for x in range(len(xr_new))] )#, ",", [mu_new[x] for x in mu_new], ",", [nu_new[x] for x in nu_new])


            # print("######", xr_new, xr_old)
            xr_old = xr_new.copy()
            xr_bar_old = xr_bar_new.copy()
            # print("######", xr_new, xr_old)
            nu_old = nu_new.copy()
            mu_old = mu_new.copy()
            beta_old = beta_new.copy()

            if round == MAX_ROUNDS:
                break
            else:
                round+=1
            
        mu_modified = defaultdict(dict)
        nu_modified = defaultdict(dict)
        for key in mu_new:
            mu_modified[key[0]][key[1]] = mu_new[key]
        for key in nu_new:
            nu_modified[key[0]][key[1]] = nu_new[key]


        with open("mu.json", 'w') as file:
            json.dump(mu_modified, file, indent=4)
        
        with open("nu.json", 'w') as file:
            json.dump(nu_modified, file, indent=4)


        # return self._solver.solve_lp(num_threads=num_threads)

    def pre_solve(self, problem=None):
        if problem is None:
            problem = self.problem

        self.commodity_list = (
            problem.sparse_commodity_list
            if self._warm_start_mode
            else problem.commodity_list
        )

        self.commodities = []
        edge_to_paths = defaultdict(list)
        self._path_to_commod = {}
        self._all_paths = []

        paths_dict = self.get_paths(problem)
        # print("self.commodity_list", self.commodity_list)
        # print("self.paths_dict", paths_dict)
        
        path_i = 0
        for k, (s_k, t_k, d_k) in self.commodity_list:
            paths = paths_dict[(s_k, t_k)]
            path_ids = []
            for path in paths:
                self._all_paths.append(path)

                for edge in path_to_edge_list(path):
                    edge_to_paths[edge].append(path_i)
                path_ids.append(path_i)

                self._path_to_commod[path_i] = k
                path_i += 1

            self.commodities.append((k, d_k, path_ids))
        if self.DEBUG:
            assert len(self._all_paths) == path_i

        self._print("pre_solve done")
        return dict(edge_to_paths), path_i

    def _construct_lp(self, sat_flows=[]):
        edge_to_paths, num_paths = self.pre_solve()
        # print("Edges to paths", edge_to_paths)
        return self._construct_path_lp(
            self._problem.G, edge_to_paths, num_paths, sat_flows
        )

    @property
    def sol_dict(self):
        if not hasattr(self, "_sol_dict"):
            sol_dict_def = defaultdict(list)
            for var in self.model.getVars():
                if var.varName.startswith("f[") and var.x != 0.0:
                    match = re.match(r"f\[(\d+)\]", var.varName)
                    p = int(match.group(1))
                    sol_dict_def[self.commodity_list[self._path_to_commod[p]]] += [
                        (edge, var.x) for edge in path_to_edge_list(self._all_paths[p])
                    ]

            # Set zero-flow commodities to be empty lists
            self._sol_dict = {}
            sol_dict_def = dict(sol_dict_def)
            for commod_key in self.problem.commodity_list:
                if commod_key in sol_dict_def:
                    self._sol_dict[commod_key] = sol_dict_def[commod_key]
                else:
                    self._sol_dict[commod_key] = []

        return self._sol_dict

    @property
    def sol_mat(self):
        edge_idx = self.problem.edge_idx
        sol_mat = np.zeros((len(edge_idx), len(self._path_to_commod)), dtype=np.float32)
        for var in self.model.getVars():
            if var.varName.startswith("f[") and var.x != 0.0:
                match = re.match(r"f\[(\d+)\]", var.varName)
                p = int(match.group(1))
                k = self._path_to_commod[p]
                for edge in path_to_edge_list(self._all_paths[p]):
                    sol_mat[edge_idx[edge], k] += var.x

        return sol_mat

    @classmethod
    # Return total number of fib entries and max for any node in topology
    # NOTE: problem has to have a full TM matrix
    def fib_entries(cls, problem, num_paths, edge_disjoint, dist_metric):
        assert problem.is_traffic_matrix_full
        pf = cls.new_total_flow(
            num_paths=num_paths, edge_disjoint=edge_disjoint, dist_metric=dist_metric
        )
        pf.pre_solve(problem)
        return pf.num_fib_entries_for_path_set()

    def num_fib_entries_for_path_set(self):
        self.fib_dict = defaultdict(dict)
        for k, _, path_ids in self.commodities:
            commod_id_str = "k-{}".format(k)
            src = list(path_to_edge_list(self._all_paths[path_ids[0]]))[0][0]
            # For a given TM, we would store weights for each path id. For demo
            # purposes, we just store the path ids
            self.fib_dict[src][commod_id_str] = path_ids

            for path_id in path_ids:
                for u, v in path_to_edge_list(self._all_paths[path_id]):
                    assert path_id not in self.fib_dict[u]
                    self.fib_dict[u][path_id] = v

        self.fib_dict = dict(self.fib_dict)
        fib_dict_counts = [len(self.fib_dict[k]) for k in self.fib_dict.keys()]
        return sum(fib_dict_counts), max(fib_dict_counts)

    @property
    def runtime(self):
        if not hasattr(self, "_runtime"):
            self._runtime = self._solver.model.Runtime
        return self._runtime
