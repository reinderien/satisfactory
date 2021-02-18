#!/usr/bin/env python3

import re
import shutil
from dataclasses import dataclass
from itertools import count, chain
from sys import stderr, stdout
from tempfile import NamedTemporaryFile
from typing import Iterable, List, Dict, ClassVar, Pattern, Collection

from requests import Session
import swiglpk as lp


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
        with session.get('https://satisfactory.gamepedia.com/api.php', params=params) as resp:
            resp.raise_for_status()
            data = resp.json()

        warnings = data.get('warnings')
        if warnings:
            for title, entries in warnings.items():
                for warning in entries.values():
                    print(f'{title}: {warning}', file=stderr)

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

    parse_re: ClassVar[Pattern] = re.compile(r'{{CraftingTable(.+?)}}', re.I | re.S)

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
        '''
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
        '''

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
        
        	    Miner Mk.1	Miner Mk.2	Miner Mk.3
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
                crafted_in=attrs["craftedIn"],
                tier=attrs['researchTier'],
                time=float(attrs['craftingTime']),
                rates={
                    attrs['product']: rate,
                },
            )


def fill_missing(session: Session, recipes: Collection[Recipe]) -> Iterable[Recipe]:
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


def get_recipes() -> List[Recipe]:
    with Session() as session:
        component_text, = get_api(session, titles='Template:ItemNav')
        component_names = parse_template(component_text, tier_before=3)
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

    return recipes


def setup_linprog(recipes: Collection[Recipe], fixed_recipes: Dict[str, int]):
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

    A variable is called non-basic if it has an active bound; otherwise it is called basic.

    For us:
    - structural variables are all integers, count of each recipe instance
    - auxiliary variables are individual resource rates
    - no resource rate may be below zero or the solution will be unsustainable
    - selected recipes will be fixed (set to 1)
    """
    resources = sorted({p for r in recipes for p in r.rates.keys()})
    resource_indices: Dict[str, int] = {r: i for i, r in enumerate(resources, 1)}
    m = len(resources)
    n = len(recipes)

    problem = lp.glp_create_prob()
    lp.glp_set_prob_name(problem, 'satisfactory')
    lp.glp_set_obj_name(problem, 'n_buildings')
    lp.glp_set_obj_dir(problem, lp.GLP_MIN)
    lp.glp_add_rows(problem, m)
    lp.glp_add_cols(problem, n)

    for i, resource in enumerate(resources, 1):
        lp.glp_set_row_name(problem, i, resource)
        lp.glp_set_row_bnds(
            problem, i,
            type=lp.GLP_LO,
            lb=0, ub=float('inf'),
        )

    for j, recipe in enumerate(recipes, 1):
        lp.glp_set_col_name(problem, j, recipe.name)
        lp.glp_set_col_kind(problem, j, lp.GLP_IV)
        lp.glp_set_obj_coef(problem, j, 1)

        fixed = fixed_recipes.get(recipe.name)
        if fixed is None:
            lp.glp_set_col_bnds(
                problem, j,
                type=lp.GLP_LO,
                lb=0, ub=float('inf'),
            )
        else:
            lp.glp_set_col_bnds(
                problem, j,
                type=lp.GLP_FX,
                lb=fixed, ub=fixed,
            )

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


def print_soln(problem, kind: str='sol'):
    fun = getattr(lp, f'glp_print_{kind}')

    with NamedTemporaryFile(mode='rt') as tempf:
        assert 0 == fun(problem, fname=tempf.name)
        shutil.copyfileobj(tempf, stdout)


def solve_linprog(problem):
    print()
    check_lp(lp.glp_simplex(problem, None))

    print()
    check_lp(lp.glp_intopt(problem, None))
    print_soln(problem, 'mip')


def main():
    print('Fetching recipe data...')
    recipes = get_recipes()

    problem = setup_linprog(
        recipes,
        {
            'Modular Frame': 1,
            'Rotor': 1,
            'Smart Plating': 1,
        },
    )

    solve_linprog(problem)


main()
