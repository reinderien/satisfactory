#!/usr/bin/env python3
import logging
import pickle
import re
from dataclasses import dataclass
from itertools import count, chain
from pathlib import Path
from typing import ClassVar, Collection, Dict, Iterable, List, Pattern, Union, Sequence

from gekko import GEKKO
from gekko.gk_operators import GK_Operators
from gekko.gk_variable import GKVariable
from requests import Session


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

    @property
    def first_output(self) -> str:
        return next(
            resource
            for resource, rate in self.rates.items()
            if rate > 0
        )

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
        rate = rates[self.first_output]
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


def pure_sum(seq: Sequence):
    total = seq[0]
    for x in seq[1:]:
        total += x
    return total


class RecipeInstance:
    def __init__(
        self,
        recipe: Recipe,
        m: GEKKO,
        shards_each: GKVariable,
        clock_percent_each: Union[GKVariable, GK_Operators],
        suffix: str,
    ):
        self.buildings: GKVariable = m.Var(
            integer=True, lb=0, name=f'{recipe.name} buildings ({suffix})',
        )

        self.name = f'{recipe.name} ({suffix})'

        self.clock_percent_each = clock_percent_each
        self.clock_each = clock = clock_percent_each / 100
        self.shards_each = shards_each
        self.power_each: GK_Operators = clock**1.6 * recipe.base_power
        self.shard_total: GK_Operators = shards_each * self.buildings
        self.power_total: GK_Operators = self.power_each * self.buildings

        self.rates_each: Dict[str, GK_Operators] = {
            resource: clock * rate
            for resource, rate in recipe.rates.items()
        }
        self.rates_total: Dict[str, GK_Operators] = {
            resource: r*self.buildings
            for resource, r in self.rates_each.items()
        }

        m.Equation(clock_percent_each <= shards_each*50 + 100)

    def __str__(self):
        return self.name


class SolutionRecipe:
    def __init__(self, recipe: Recipe, m):
        self.recipe = recipe

        self.base_instance = base = RecipeInstance(
            recipe, m,
            shards_each=m.Var(integer=True, lb=0, ub=3, name=f'{recipe.name} shards each'),
            clock_percent_each=m.Var(integer=True, lb=0, ub=249, name=f'{recipe.name} clock each'),
            suffix='base',
        )
        self.fract_instance = fract = RecipeInstance(
            recipe, m,
            shards_each=base.shards_each,
            clock_percent_each=base.clock_percent_each + 1,
            suffix='fract',
        )

        self.clock_total: GK_Operators = base.clock_each + fract.clock_each
        self.shard_total: GK_Operators = base.shard_total + fract.shard_total
        self.power_total: GK_Operators = base.power_total + fract.power_total
        self.building_total: GK_Operators = base.buildings + fract.buildings
        self.rates_total: Dict[str, GK_Operators] = {
            resource: base_rate + fract.rates_total[resource]
            for resource, base_rate in base.rates_total.items()
        }

    def __str__(self):
        return self.recipe.name


class Solver:
    def __init__(self, recipes: Dict[str, Recipe]):
        # No network; discontinuous problem; respect integer constraints
        APOPT = 1
        m = self.m = GEKKO(remote=False, name='satisfactory_power')
        m.options.solver = APOPT
        m.solver_options = ['minlp_as_nlp 0']

        self.recipes: Dict[str, SolutionRecipe] = {
            name: SolutionRecipe(r, m)
            for name, r in recipes.items()
        }

        self.shards_total = pure_sum([r.shard_total for r in self.recipes.values()])
        self.power_total = pure_sum([r.power_total for r in self.recipes.values()])
        self.building_total = pure_sum([r.building_total for r in self.recipes.values()])

        self.rates: Dict[str, GK_Operators] = {}
        for recipe in self.recipes.values():
            for resource, rate in recipe.rates_total.items():
                if resource in self.rates:
                    self.rates[resource] += rate
                else:
                    self.rates[resource] = rate

        for rate in self.rates.values():
            m.Equation(rate >= 0)

    def constraints(self, *args: GK_Operators):
        for arg in args:
            self.m.Equation(arg)

    def solve(self):
        self.m.solve(disp=logger.level <= logging.DEBUG)

    def print(self):
        # todo - broken
        print(
            f'{"Recipe":40} '
            f'{"Clock":5} '
            f'{"n":>2} '
            
            f'{"P (MW)":>6} {"tot":>6} '
            f'{"shards":>6} {"tot":>3} '
            f'{"s/out":>5} {"tot":>4} {"s/extra":>7}'
        )

        for s in self.recipes:
            print(
                f'{s.recipe.name:40} '
                f'{s.clock_each:>5.0f} '
                f'{s.n:>2} '
                
                f'{s.power_each/1e6:>6.2f} {s.power_total/1e6:>6.2f} '
                f'{s.shards_each:>6} {s.shards_total:>3} '
                f'{s.secs_per_output_each:>5.1f} {s.secs_per_output_total:>4.1f} '
                f'{s.recipe.secs_per_extra(self.rates):>7}'
            )

        print(
            f'{"Total":40} '
            f'{"":5} {self.total_buildings:>2} '
            f'{"":6} {self.total_power/1e6:>6.2f} '
            f'{"":6} {self.total_shards:>3}'
        )


def main():
    logger.level = logging.DEBUG
    logger.addHandler(logging.StreamHandler())

    recipes = load_recipes(tier_before=3)
    logger.info(f'{len(recipes)} recipes loaded.')

    sol = Solver(recipes)
    sol.constraints(
        sol.recipes['Modular Frame'].clock_total >= 1,
        sol.recipes['Rotor'].clock_total >= 1,
        sol.recipes['Smart Plating'].clock_total >= 1,
        sol.building_total <= 50,
        # sol.power_total <= 100e6,
    )
    # todo - objective
    sol.solve()
    sol.print()


if __name__ == '__main__':
    main()
