import logging
import os
from tempfile import NamedTemporaryFile
from typing import Dict, Iterable, Tuple, TYPE_CHECKING, TypeVar, Optional

import swiglpk as lp

from .logs import logger

if TYPE_CHECKING:
    from .recipe import Recipe


SwigPyObject = TypeVar('SwigPyObject')


"""
x[m+i] ("xs") is an n-vector, structural variables ("columns")
z is the scalar cost/objective function to minimize
c is an (n+1)-vector, objective coefficients; will be set to ones

        n
z = [c c c...][xs]
              [xs]   n
              [xs]
              [...]

x ("xr") is an m-vector, auxiliary variables ("rows")
a is an m*n constraint coefficient matrix

auxiliary variables are set by
    n        1        1
  [a a a]   [xs]     [xr]
m [a a a] n [xs] = m [xr]
  [a a a]   [xs]     [xr]

bounds apply to both structural and auxiliary variables:
l ≤ x ≤ u

A variable is called non-basic if it has an active bound; otherwise it is
called basic.

For us:
- structural variables are all integers, one percent of each recipe instance -
  the sum of all of these will be minimized
- auxiliary variables are individual resource rates (also scaled by 0.01)
- no resource rate may be below zero or the solution will be unsustainable
- selected recipes will be fixed to a desired output percentage
"""


def setup_row(
    problem: SwigPyObject,
    i: int,
    resource: str,
    min_rate: float,
):
    lp.glp_set_row_name(problem, i, resource)

    # Every resource rate must be 0 or greater for sustainability
    lp.glp_set_row_bnds(
        problem, i,
        lp.GLP_LO,
        100*min_rate, float('inf'),  # Lower and upper boundaries
    )


def setup_col(
    problem: SwigPyObject,
    j: int,
    recipe: 'Recipe',
    resource_indices: Dict[str, int],
    min_clock: Optional[int] = None,
    fixed_clock: Optional[int] = None,
):
    lp.glp_set_col_name(problem, j, recipe.name)

    # The game's clock scaling resolution is one percentage point, so we
    # ask for integers with an implicit scale of 100
    lp.glp_set_col_kind(problem, j, lp.GLP_IV)

    # All recipes are currently weighed the same
    lp.glp_set_obj_coef(problem, j, 1)

    if fixed_clock is None:
        # All recipes must have at least 0 instances
        lp.glp_set_col_bnds(
            problem, j,
            lp.GLP_LO,
            min_clock or 0, float('inf'),  # Lower and upper boundaries
        )
    else:
        # Set our desired (fixed) outputs
        lp.glp_set_col_bnds(
            problem, j,
            lp.GLP_FX,
            fixed_clock, fixed_clock,  # Boundaries are equal (variable is fixed)
        )

    # The constraint coefficients are just the recipe rates
    n_sparse = len(recipe.rates)
    ind = lp.intArray(n_sparse + 1)
    val = lp.doubleArray(n_sparse + 1)
    for i, (resource, rate) in enumerate(recipe.rates.items(), 1):
        ind[i] = resource_indices[resource]
        val[i] = rate
    lp.glp_set_mat_col(problem, j, n_sparse, ind, val)


def setup_linprog(
    recipes: Dict[str, 'Recipe'],
    min_rates: Optional[Dict[str, float]] = None,
    min_clocks: Optional[Dict[str, int]] = None,
    fixed_clocks: Optional[Dict[str, int]] = None,
) -> SwigPyObject:
    if min_rates is None:
        min_rates = {}
    if min_clocks is None:
        min_clocks = {}
    if fixed_clocks is None:
        fixed_clocks = {}

    resources = sorted({p for r in recipes.values() for p in r.rates.keys()})
    resource_indices: Dict[str, int] = {
        r: i
        for i, r in enumerate(resources, 1)
    }
    m = len(resources)
    n = len(recipes)

    problem: SwigPyObject = lp.glp_create_prob()
    lp.glp_set_prob_name(problem, 'satisfactory')
    lp.glp_set_obj_name(problem, 'percentage_sum')
    lp.glp_set_obj_dir(problem, lp.GLP_MIN)
    lp.glp_add_rows(problem, m)
    lp.glp_add_cols(problem, n)

    for i, resource in enumerate(resources, 1):
        setup_row(problem, i, resource, min_rates.get(resource, 0))

    for j, recipe in enumerate(recipes.values(), 1):
        setup_col(
            problem, j, recipe, resource_indices,
            min_clocks.get(recipe.name), fixed_clocks.get(recipe.name),
        )

    lp.glp_create_index(problem)

    return problem


def check_lp(problem: SwigPyObject, code: int):
    msg = ''

    statuses = {
        getattr(lp, f'GLP_{stat}'): stat
        for stat in (
            'OPT',
            'FEAS',
            'INFEAS',
            'NOFEAS',
            'UNBND',
            'UNDEF',
        )
    }

    generic, primal, dual = (
        getattr(lp, f'glp_get_{fun}')(problem)
        for fun in (
            'status',
            'prim_stat',
            'dual_stat',
        )
    )

    if (
        generic != lp.GLP_OPT
        or primal != lp.GLP_FEAS
        or dual != lp.GLP_FEAS
    ):
        msg += (
            'Mehrotra feasibility constraints failed: '
            f'generic {statuses[generic]}, '
            f'primal {statuses[primal]}, '
            f'dual {statuses[dual]}'
        )

    if code != 0:
        codes = {
            getattr(lp, k): k
            for k in dir(lp)
            if k.startswith('GLP_E')
        }
        msg += f'gltk returned {codes[code]}'

    if msg:
        raise ValueError(msg)


def log_soln(problem: SwigPyObject, kind: str):
    if logger.level > logging.DEBUG:
        return

    fun = getattr(lp, f'glp_print_{kind}')

    tempf = NamedTemporaryFile(mode='w', delete=False)
    try:
        tempf.close()
        assert 0 == fun(problem, tempf.name)
        with open(tempf.name, 'rt') as f:
            logger.debug(f.read())
    finally:
        os.unlink(tempf.name)


def level_for_parm() -> int:
    # It's difficult (impossible?) to redirect stdout from this DLL, so just
    # modify its verbosity to follow that of our own logger

    if logger.level <= logging.DEBUG:
        return lp.GLP_MSG_ALL
    # Skip over ON; too verbose
    # if logger.level <= logging.INFO:
    #    return lp.GLP_MSG_ON
    if logger.level <= logging.ERROR:
        return lp.GLP_MSG_ERR
    return lp.GLP_MSG_OFF


def solve_linprog(problem: SwigPyObject):
    level = level_for_parm()

    parm = lp.glp_smcp()
    lp.glp_init_smcp(parm)
    parm.msg_lev = level
    check_lp(problem, lp.glp_simplex(problem, parm))
    log_soln(problem, 'sol')

    parm = lp.glp_iocp()
    lp.glp_init_iocp(parm)
    parm.msg_lev = level
    check_lp(problem, lp.glp_intopt(problem, parm))
    log_soln(problem, 'mip')


def get_rates(problem: SwigPyObject) -> Iterable[Tuple[str, float]]:
    for i in range(1, 1 + lp.glp_get_num_rows(problem)):
        yield (
            lp.glp_get_row_name(problem, i),
            lp.glp_mip_row_val(problem, i) / 100,
        )


def get_clocks(problem: SwigPyObject) -> Iterable[Tuple[str, float]]:
    n_recipes = 0
    for j in range(1, 1 + lp.glp_get_num_cols(problem)):
        clock = lp.glp_mip_col_val(problem, j)
        if clock:
            name = lp.glp_get_col_name(problem, j)
            yield name, clock
            n_recipes += 1

    logger.info(f'{n_recipes} recipes in rate solution.')
