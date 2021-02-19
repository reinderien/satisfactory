#!/usr/bin/env python3
import logging
import os
import pickle
import re
from dataclasses import dataclass
from itertools import count, chain
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import ClassVar, Collection, Dict, Iterable, List, Pattern, Tuple, Optional

from gekko import GEKKO
from requests import Session
import swiglpk as lp


logger = logging.getLogger('satisfactory')


def get_api(session: Session, **kwargs) -> Iterable[str]:
    params = {
        'action': 'query',
        'prop': 'revisions',
        'rvprop': 'content',
        'rvslots': '*',
        'format': 'json',
        **kwargs,
    }

    while True:
        with session.get(
            'https://satisfactory.gamepedia.com/api.php', params=params,
        ) as resp:
            resp.raise_for_status()
            data = resp.json()

        warnings = data.get('warnings')
        if warnings:
            for title, entries in warnings.items():
                for warning in entries.values():
                    logger.warning(f'%s: %s', title, warning)

        for page in data['query']['pages'].values():
            revision, = page['revisions']
            yield revision['slots']['main']['*']

        if 'batchcomplete' in data:
            break

        params.update(data['continue'])


def parse_template(content: str, tier_before: int) -> List[str]:
    start = content.index('\n| group2     = Components\n')
    end = content.index(f'[[Tier {tier_before}]]', start)

    return re.findall(
        r'(?<={{ItemLink\|)'
        r'[\w\s]+',
        content[start: end],
    )


@dataclass
class Recipe:
    name: str
    crafted_in: str
    tier: str
    time: float

    rates: Dict[str, float]

    parse_re: ClassVar[Pattern] = re.compile(
        r'{{CraftingTable(.+?)}}', re.I | re.S,
    )

    BASE_POWERS: ClassVar[Dict[str, float]] = {
        'Smelter': 4e6,
        'Constructor': 4e6,
        'Assembler': 15e6,
        'Miner Mk. 1': 5e6,
        'Miner Mk. 2': 12e6,
        'Miner Mk. 3': 30e6,
    }

    @property
    def base_power(self) -> float:
        return self.BASE_POWERS[self.crafted_in]

    def __str__(self):
        return self.name

    @classmethod
    def get_attrs(cls, page: str) -> Iterable[Dict[str, str]]:
        for match in cls.parse_re.finditer(page):
            recipe = match[1]
            yield dict(
                tuple(elm.strip() for elm in kv.split('='))
                for kv in recipe.split('|')[1:]
            )

    @classmethod
    def from_component_page(cls, page: str) -> Iterable['Recipe']:
        """
        {{CraftingTable
        | product = Cable
        | recipeName = Cable
        | researchTier = [[Tier 0]] - HUB Upgrade 2
        | craftedIn = Constructor
        | inCraftBench = 1
        | productCount = 1        (output per crafting time)
        | craftingTime = 2        (seconds)
        | craftingClicks = 1
        | quantity1 = 2           (input per crafting time)
        | ingredient1 = Wire
        }}
        """

        for attrs in cls.get_attrs(page):
            if attrs.get('alternateRecipe') == '1':
                continue

            t = float(attrs['craftingTime'])
            rates = {
                attrs['product']: float(attrs['productCount'])/t,
            }

            for i in count(1):
                name = attrs.get(f'ingredient{i}')
                if name:
                    rates[name] = -float(attrs[f'quantity{i}'])/t
                else:
                    break

            yield Recipe(
                name=attrs['recipeName'],
                crafted_in=attrs['craftedIn'],
                tier=attrs['researchTier'],
                time=t,
                rates=rates,
            )

    @classmethod
    def from_ore_page(cls, page: str, max_miner: int = 1) -> Iterable['Recipe']:
        if '[[Category:Ores]]' not in page:
            return ()

        '''
        The page contains
        
        {{CraftingTable
        | product = Copper Ore
        | recipeName = Copper Ore
        | researchTier = [[Tier 0]] - HUB Upgrade 2
        | craftedIn = Miner
        | productCount = 1
        | craftingTime = 1
        }}
        
        but in all ore cases this translates to
        
                Miner Mk.1  Miner Mk.2  Miner Mk.3
        Impure	30	60	120
        Normal	60	120	240
        Pure	120	240	480
        '''

        attrs = next(cls.get_attrs(page))

        for mark, purity, rate in (
            (1, 'Impure', 0.5),
            (1, 'Normal', 1.0),
            (1, 'Pure',   2.0),
            (2, 'Impure', 1.0),
            (2, 'Normal', 2.0),
            (2, 'Pure',   4.0),
            (3, 'Impure', 2.0),
            (3, 'Normal', 4.0),
            (3, 'Pure',   8.0),
        ):
            if mark > max_miner:
                break
            crafted_in = f'{attrs["craftedIn"]} Mk. {mark}'
            yield cls(
                name=f'{attrs["recipeName"]} from '
                     f'{crafted_in} on {purity} node',
                crafted_in=crafted_in,
                tier=attrs['researchTier'],
                time=float(attrs['craftingTime']),
                rates={
                    attrs['product']: rate,
                },
            )

    def secs_per_extra(self, rates: Dict[str, float]) -> str:
        first_output = next(
            resource
            for resource, rate in self.rates.items()
            if rate > 0
        )
        rate = rates[first_output]
        if rate < 1e-6:
            return '∞'
        return f'{1/rate:.1f}'


def fill_missing(
    session: Session, recipes: Collection[Recipe]
) -> Iterable[Recipe]:
    known_products = set()
    all_inputs = set()
    for recipe in recipes:
        for product, quantity in recipe.rates.items():
            if quantity > 0:
                known_products.add(product)
            elif quantity < 0:
                all_inputs.add(product)

    missing_input_names = all_inputs - known_products
    inputs = get_api(session, titles='|'.join(missing_input_names))

    for missing_input in inputs:
        yield from Recipe.from_ore_page(missing_input)


def get_recipes(tier_before: int) -> Dict[str, Recipe]:
    with Session() as session:
        component_text, = get_api(session, titles='Template:ItemNav')
        component_names = parse_template(component_text, tier_before)
        component_pages = get_api(
            session,
            titles='|'.join(component_names),
            # rvsection=2,  # does not work for Biomass
        )

        recipes = list(chain.from_iterable(
            Recipe.from_component_page(page)
            for page in component_pages
        ))
        recipes.extend(fill_missing(session, recipes))

    return {r.name: r for r in recipes}


def load_recipes(tier_before: int) -> Dict[str, Recipe]:
    logger.info(f'Loading recipe database up to tier {tier_before-1}...')

    fn = Path('.recipes')
    if fn.exists():
        with fn.open('rb') as f:
            return pickle.load(f)

    logger.info('Fetching recipe data from MediaWiki...')
    recipes = get_recipes(tier_before)
    with fn.open('wb') as f:
        pickle.dump(recipes, f)
    return recipes


def setup_linprog(
    recipes: Dict[str, Recipe],
    fixed_recipe_percentages: Dict[str, int],
):
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
    - structural variables are all integers, one percent of each recipe instance
    - auxiliary variables are individual resource rates (also scaled by 0.01)
    - no resource rate may be below zero or the solution will be unsustainable
    - selected recipes will be fixed to a desired output percentage
    """
    resources = sorted({p for r in recipes.values() for p in r.rates.keys()})
    resource_indices: Dict[str, int] = {
        r: i
        for i, r in enumerate(resources, 1)
    }
    m = len(resources)
    n = len(recipes)

    problem = lp.glp_create_prob()
    lp.glp_set_prob_name(problem, 'satisfactory')
    lp.glp_set_obj_name(problem, 'percentage_sum')
    lp.glp_set_obj_dir(problem, lp.GLP_MIN)
    lp.glp_add_rows(problem, m)
    lp.glp_add_cols(problem, n)

    for i, resource in enumerate(resources, 1):
        lp.glp_set_row_name(problem, i, resource)

        # Every resource rate must be 0 or greater for sustainability
        lp.glp_set_row_bnds(
            problem, i,
            lp.GLP_LO,
            0, float('inf'),  # Lower and upper boundaries
        )

    for j, recipe in enumerate(recipes.values(), 1):
        lp.glp_set_col_name(problem, j, recipe.name)

        # The game's clock scaling resolution is one percentage point, so we
        # ask for integers with an implicit scale of 100
        lp.glp_set_col_kind(problem, j, lp.GLP_IV)

        # All recipes are currently weighed the same
        lp.glp_set_obj_coef(problem, j, 1)

        fixed = fixed_recipe_percentages.get(recipe.name)
        if fixed is None:
            # All recipes must have at least 0 instances
            lp.glp_set_col_bnds(
                problem, j,
                lp.GLP_LO,
                0, float('inf'),  # Lower and upper boundaries
            )
        else:
            # Set our desired (fixed) outputs
            lp.glp_set_col_bnds(
                problem, j,
                lp.GLP_FX,
                fixed, fixed,  # Boundaries are equal (variable is fixed)
            )

        # The constraint coefficients are just the recipe rates
        n_sparse = len(recipe.rates)
        ind = lp.intArray(n_sparse + 1)
        val = lp.doubleArray(n_sparse + 1)
        for i, (resource, rate) in enumerate(recipe.rates.items(), 1):
            ind[i] = resource_indices[resource]
            val[i] = rate
        lp.glp_set_mat_col(problem, j, n_sparse, ind, val)

    lp.glp_create_index(problem)

    return problem


def check_lp(code: int):
    if code == 0:
        return

    codes = {
        getattr(lp, k): k
        for k in dir(lp)
        if k.startswith('GLP_E')
    }
    raise ValueError(f'gltk returned {codes[code]}')


def log_soln(problem, kind: str):
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
    if logger.level <= logging.INFO:
        return lp.GLP_MSG_ERR  # Skip over ON; too verbose
    if logger.level <= logging.ERROR:
        return lp.GLP_MSG_ERR
    return lp.GLP_MSG_OFF


def solve_linprog(problem):
    level = level_for_parm()

    parm = lp.glp_smcp()
    lp.glp_init_smcp(parm)
    parm.msg_lev = level
    check_lp(lp.glp_simplex(problem, parm))
    log_soln(problem, 'sol')

    parm = lp.glp_iocp()
    lp.glp_init_iocp(parm)
    parm.msg_lev = level
    check_lp(lp.glp_intopt(problem, parm))
    log_soln(problem, 'mip')


def get_rates(problem) -> Iterable[Tuple[str, float]]:
    for i in range(1, 1 + lp.glp_get_num_rows(problem)):
        yield (
            lp.glp_get_row_name(problem, i),
            lp.glp_mip_row_val(problem, i) / 100,
        )


def get_clocks(problem) -> Iterable[Tuple[str, float]]:
    for j in range(1, 1 + lp.glp_get_num_cols(problem)):
        clock = lp.glp_mip_col_val(problem, j)
        if clock:
            name = lp.glp_get_col_name(problem, j)
            yield name, clock


@dataclass
class SolvedRecipe:
    recipe: Recipe
    n: int
    clock_total: int

    @property
    def clock_each(self) -> float:
        return self.clock_total / self.n

    @property
    def power_each(self) -> float:
        return self.recipe.base_power * (self.clock_each/100) ** 1.6

    @property
    def power_total(self) -> float:
        return self.power_each * self.n

    def __str__(self):
        return f'{self.recipe} ×{self.n}'

    @classmethod
    def solve_all(
        cls,
        recipes: Dict[str, Recipe],
        percentages: Dict[str, float],
        max_buildings: Optional[int] = None,
        max_power: Optional[float] = None,
    ) -> List['SolvedRecipe']:
        """
        At this point, we have a total percentage for each recipe, but no choice
        on allocation of those percentages to a building count, considering
        nonlinear power load and the addition of power shards.

        For a given maximum building count, there will be an optimal clock
        scaling configuration, building allocation and power shard allocation
        that minimizes power consumption.
        """

        rate_items: Collection[Tuple[Recipe, float]] = [
            (recipes[recipe], rate)
            for recipe, rate in percentages.items()
        ]

        # No network; discontinuous problem; respect integer constraints
        APOPT = 1
        m = GEKKO(remote=False, name='satisfactory_power')
        m.options.solver = APOPT
        m.solver_options = ['minlp_as_nlp 0']

        buildings = [
            m.Var(integer=True, name=recipe.name, lb=1)
            for recipe, rate in rate_items
        ]
        powers = [
            build_var**-0.6 * (clock/100)**1.6 * recipe.base_power
            for build_var, (recipe, clock) in zip(buildings, rate_items)
        ]

        for (recipe, clock), building in zip(rate_items, buildings):
            m.Equation(clock/building <= 250)

        if max_buildings is not None:
            logger.info(f'Minimizing power for at most {max_buildings} buildings:')
            m.Equation(sum(buildings) <= max_buildings)
            m.Minimize(sum(powers))
        elif max_power is not None:
            logger.info(f'Minimizing buildings for at most {max_power/1e6:.0f} MW power:')
            m.Equation(sum(powers) <= max_power)
            m.Minimize(sum(buildings))
        else:
            raise ValueError('Need a limit')

        m.solve(disp=logger.level <= logging.DEBUG)

        solved = (
            cls(
                recipe=recipe,
                n=int(building.value[0]),
                clock_total=int(clock),
            ).distribute()
            for building, (recipe, clock) in zip(buildings, rate_items)
        )
        return list(chain.from_iterable(solved))

    def distribute(self) -> Tuple['SolvedRecipe', ...]:
        quo, rem = divmod(self.clock_total, self.n)
        if not rem:
            return self,

        y = self.clock_total - quo*self.n
        x = self.n - y

        return (
            SolvedRecipe(self.recipe, x, x*quo),
            SolvedRecipe(self.recipe, y, y*(quo+1)),
        )


def print_power(solved: Collection[SolvedRecipe], rates: Dict[str, float]):
    print(
        f'{"Recipe":40} '
        f'{"Clock":5} '
        f'{"n":>2} '
        f'{"P (MW)":>6} '
        f'{"Ptot":>6} '
        f'{"s/extra":>7}'
    )

    for s in solved:
        print(
            f'{s.recipe.name:40} '
            f'{s.clock_each:>5.0f} '
            f'{s.n:>2} '
            f'{s.power_each/1e6:>6.2f} '
            f'{s.power_total/1e6:>6.2f} '
            f'{s.recipe.secs_per_extra(rates):>7}'
        )

    print(
        f'{"Total":40} '
        f'{"":5} '
        f'{sum(s.n for s in solved):>2} '
        f'{"":6} '
        f'{sum(s.power_total for s in solved)/1e6:>6.2f} '
    )


def main():
    logger.level = logging.INFO
    logger.addHandler(logging.StreamHandler())

    recipes = load_recipes(tier_before=3)
    logger.info(f'{len(recipes)} recipes loaded.')

    logger.info('Linear stage...')
    problem = setup_linprog(
        recipes,
        {
            'Modular Frame': 100,
            'Rotor': 100,
            'Smart Plating': 100,
        },
    )
    solve_linprog(problem)
    percentages = dict(get_clocks(problem))
    rates = dict(get_rates(problem))
    logger.info(f'{len(percentages)} recipes in solution.')

    logger.info('Nonlinear stage...')
    solved = SolvedRecipe.solve_all(recipes, percentages, max_buildings=50)

    print_power(solved, rates)


if __name__ == '__main__':
    main()
