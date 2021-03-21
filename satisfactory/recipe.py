import math
import pickle
import re
from collections import defaultdict
from dataclasses import dataclass
from gekko.gk_operators import GK_Intermediate

from graphviz import Digraph
from itertools import count, chain
from pathlib import Path
from typing import ClassVar, Dict, Iterable, Pattern, Set, Collection, Optional, Callable, Tuple

from requests import Session

from .logs import logger
from .mediawiki import parse_template, get_api, fetch_recipes, fetch_powers


@dataclass
class Rate:
    resource: str
    time: float
    quantity: float
    exp: float = 1

    @property
    def rate(self) -> float:
        return self.quantity / self.time

    def scaled_rate(self, clock: int) -> float:
        scale = clock / 100
        if False:  # not self.is_linear:
            scale **= self.exp
        return scale * self.rate

    def clock_for_rate(self, rate: float) -> float:
        return (rate/self.rate) ** (1/self.exp) * 100

    @property
    def is_input(self) -> bool:
        return self.rate < 0

    @property
    def is_output(self) -> bool:
        return self.rate > 0

    @property
    def is_linear(self) -> bool:
        return self.exp == 1


@dataclass
class Recipe:
    name: str
    crafted_in: str
    tier: str
    time: float
    rates: Dict[str, Rate]

    _attr_re: ClassVar[Pattern] = re.compile(
        r'^'
        r' *\|'
        r' *([^|=]+?)'
        r' *='
        r' *(.*?)'
        r' *$',
        re.M,
    )

    @property
    def first_output(self) -> str:
        return next(
            resource
            for resource, rate in self.rates.items()
            if rate.is_output
        )

    def __str__(self):
        return self.name

    @classmethod
    def get_attrs(cls, page: str, box_name: str) -> Iterable[Dict[str, str]]:
        parse = (
            r'{{' + re.escape(box_name) +
            r'(.+?)\n'
            r'}}'
        )

        for match in re.finditer(parse, page, re.I | re.S):
            recipe = match[1]
            d = dict(
                m.groups()
                for m in cls._attr_re.finditer(recipe)
            )
            yield d

    def secs_per_extra(self, rates: Dict[str, GK_Intermediate]) -> str:
        rate, = rates[self.first_output]
        if rate < 1e-6:
            return '∞'
        return f'{1 / rate:.1f}'

    @property
    def building_name(self) -> str:
        if 'Miner' in self.crafted_in:
            return self.name.split('from ')[1]
        return self.crafted_in


class ProductionRecipe(Recipe):
    @classmethod
    def from_page(cls, page: str) -> Iterable['ProductionRecipe']:
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

        for attrs in cls.get_attrs(page, 'CraftingTable'):
            if attrs.get('alternateRecipe') == '1':
                continue

            t = float(attrs['craftingTime'])
            name = attrs['product']
            rates = {
                name: Rate(
                    resource=name,
                    quantity=float(attrs['productCount']),
                    time=t,
                )
            }

            for i in count(1):
                name = attrs.get(f'ingredient{i}')
                if name:
                    rates[name] = Rate(
                        resource=name,
                        quantity=-float(attrs[f'quantity{i}']),
                        time=t,
                    )
                else:
                    break

            yield Recipe(
                name=attrs['recipeName'],
                crafted_in=attrs['craftedIn'],
                tier=attrs['researchTier'],
                time=t,
                rates=rates,
            )


@dataclass
class OreRecipe(Recipe):
    mark: int
    purity: str

    @classmethod
    def from_page(cls, page: str) -> Iterable['OreRecipe']:
        if not any(
            ore_str in page
            for ore_str in (
                '[[Category:Ores]]',
                '| category = Ores',
            )
        ):
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

        attrs = next(cls.get_attrs(page, 'CraftingTable'))
        resource = attrs['recipeName']
        t = float(attrs['craftingTime'])
        quantity = float(attrs['productCount'])

        for purity_name, purity_mod in (
            ('Impure', 0.5),
            ('Normal', 1.0),
            ('Pure',   2.0),
        ):
            for mark, mark_mod in (
                (1, 1),
                (2, 2),
                (3, 4),
            ):
                crafted_in = f'{attrs["craftedIn"]} Mk.{mark}'
                rate = purity_mod * mark_mod
                final_t = t/rate

                yield cls(
                    mark=mark,
                    purity=purity_name,
                    name=f'{resource} from {crafted_in} '
                         f'on {purity_name} node',
                    crafted_in=crafted_in,
                    tier=attrs['researchTier'],
                    time=final_t,
                    rates={
                        attrs['product']: Rate(
                            resource=resource,
                            quantity=quantity,
                            time=final_t,
                        ),
                    },
                )


class GeneratorRecipe(Recipe):
    _fuel_re: ClassVar[Pattern] = re.compile(
        r'(?<=\[\[)'
        r'.+?'
        r'(?=\]\])'
    )

    def for_coal(page: str) -> Iterable[Tuple[str, float]]:
        """
        {| class="wikitable"
        ! Fuel type !! Energy (MJ) !! Stack size !! '''Stack energy (MJ)''' !! Burn time (sec) !! '''Items per minute'''
        |-
        | {{ItemLink|Coal}} || 300 || 100 || 30,000 || 4 || 15
        |-
        | {{ItemLink|Compacted Coal}} || 630 || 100 || 63,000 || 8.4 || 7.143
        |-
        | {{ItemLink|Petroleum Coke}} || 180 || 200 || 36,000 || 2.4 || 25
        |}
        """

        start = 0
        prefix = '{| class="wikitable"\n'
        cap_re = re.compile(
            r"!+ *'*"
            r"([^!']+?)"
            r" *(!|'|$)"
        )

        while True:
            cap_start = page.index(prefix, start) + len(prefix)
            cap_end = page.index('\n', cap_start)
            cap_line = page[cap_start: cap_end]
            caps = [m[1] for m in cap_re.finditer(cap_line)]
            start = 1 + cap_end
            if caps[0] == 'Fuel type':
                break

        sep = '|-\n'
        while page[start: start + len(sep)] == sep:
            # | {{ItemLink|Coal}} || 300 || 100 || 30,000 || 4 || 15
            start += len(sep)
            line_end = page.find('\n', start)
            line = page[start+1: line_end]
            items = [i.strip() for i in line.split('||')]

            attrs = dict(zip(caps, items))
            name = attrs['Fuel type'].split('|', 1)[1].split('}', 1)[0]
            energy = float(attrs['Energy (MJ)'])
            yield name, energy

            start = 1 + line_end

    def for_fuel(page: str) -> Iterable[Tuple[str, float]]:
        """
        === {{ItemLink|Fuel}} (600 MJ/m{{cubic}}) ===
        """

        for match in re.finditer(
            r'=== {{ItemLink\|'
            r'([^|}{]+)'
            r'}} \('
            r'([0-9,]+)'
            r' MJ/m',
            page,
        ):
            yield match[1], float(match[2].replace(',', ''))

    parse_by_gen: ClassVar[Dict[str, Callable]] = {
        'Coal Generator': for_coal,
        'Fuel Generator': for_fuel,
    }

    @classmethod
    def from_page(cls, name: str, page: str, tiers: Set[str]) -> Iterable['GeneratorRecipe']:
        if (
            name in {
                'Biomass Burner',  # manual feed only
                'Power Storage',  # not an actual generator
            }
            or 'Future content' in name
        ):
            return ()

        attrs = next(cls.get_attrs(page, 'InfoBox'))
        if not any(
            tier in attrs['researchTier'] for tier in tiers
        ):
            return

        # todo - Geothermal Generator

        fuels = dict(cls.parse_by_gen[name](page))
        power = float(attrs['powerGenerated'])
        rates = {
            'Power': Rate(
                resource='Power',
                quantity=power,
                time=1,
                exp=0.77,
            ),
        }

        for fuel, energy in fuels.items():
            recipe = cls(
                name=f'{name} powered by {fuel}',
                crafted_in=name,
                tier=attrs['researchTier'],
                time=1,
                rates={
                    fuel: Rate(
                        resource=fuel,
                        quantity=-1,
                        time=energy/power,
                        exp=0.77,
                    ),
                    **rates,
                }
            )

            yield recipe


def fill_ores(
    session: Session, recipes: Collection[Recipe],
) -> Iterable[OreRecipe]:
    known_products = set()
    all_inputs = set()
    for recipe in recipes:
        for product, rate in recipe.rates.items():
            if rate.is_output:
                known_products.add(product)
            elif rate.is_input:
                all_inputs.add(product)

    missing_input_names = all_inputs - known_products
    inputs = get_api(session, titles='|'.join(missing_input_names))

    for title, missing_input in inputs:
        yield from OreRecipe.from_page(missing_input)


def fill_generators(
    session: Session,
    tiers: Set[str],
) -> Iterable[GeneratorRecipe]:
    for name, page in get_api(
        session,
        generator='categorymembers',
        gcmtitle='Category:Generators',
        gcmtype='page',
        gcmlimit=250,
    ):
        yield from GeneratorRecipe.from_page(name, page, tiers)


def get_recipes(tiers: Set[str]) -> Dict[str, Recipe]:
    with Session() as session:
        (_, component_text), = get_api(session, titles='Template:ItemNav')
        component_names = sorted(set(parse_template(component_text, tiers)))
        component_pages = fetch_recipes(session, component_names)

        recipes = list(chain.from_iterable(
            ProductionRecipe.from_page(page)
            for title, page in component_pages
        ))
        recipes.extend(fill_ores(session, recipes))

        powers = dict(fetch_powers(
            session,
            {recipe.crafted_in for recipe in recipes},
            tiers,
        ))
        generators = tuple(fill_generators(session, tiers))

    above_tier = []
    for i, recipe in enumerate(recipes):
        power = powers.get(recipe.crafted_in)
        if power is None:
            above_tier.append(i)
        else:
            recipe.rates['Power'] = Rate(
                resource='Power', quantity=-power, time=1, exp=1.6,
            )

    for i in reversed(above_tier):
        del recipes[i]

    all_outputs = {
        rate.resource
        for recipe in recipes
        for rate in recipe.rates.values()
        if rate.is_output
    }

    recipes.extend(
        g for g in generators
        if all(
            rate == 'Power' or rate in all_outputs
            for rate in g.rates.keys()
        )
    )

    return {r.name: r for r in recipes}


def load_recipes(tiers: Set[str]) -> Dict[str, Recipe]:
    logger.info(f'Loading recipe database for {len(tiers)} tiers...')

    fn = Path('.recipes')
    if fn.exists():
        with fn.open('rb') as f:
            return pickle.load(f)

    logger.info('Fetching recipe data from MediaWiki...')
    recipes = get_recipes(tiers)
    with fn.open('wb') as f:
        pickle.dump(recipes, f)
    logger.info(f'{len(recipes)} recipes loaded.')
    return recipes


def graph_recipes(
    recipes: Dict[str, Recipe],
    fn: str = 'recipes.gv',
    view: bool = True,
):
    dot = Digraph(
        name='Recipes for selected tiers',
        filename=fn,
    )

    resources = set()
    for recipe in recipes.values():
        resources.update(recipe.rates.keys())

    labels_to_i = {
        label: str(i)
        for i, label in enumerate(resources)
    }

    for label, i in labels_to_i.items():
        dot.node(name=i, label=label)

    for recipe in recipes.values():
        for source, rate in recipe.rates.items():
            if rate.is_input:
                dot.edge(
                    tail_name=labels_to_i[source],
                    head_name=labels_to_i[recipe.first_output],
                    label=f'{-1/rate.rate:.2f} s/1',
                )

    dot.render(view=view)


def prune_recipes(
    recipes: Dict[str, 'Recipe'], initial_recipes: Dict[str, float],
) -> Tuple[
    Dict[str, 'Recipe'],  # pruned recipes
    Dict[str, float],  # initial clocks
]:
    recipes_by_resource = defaultdict(list)
    for recipe_name, recipe in recipes.items():
        for resource, rate in recipe.rates.items():
            if rate.is_output:
                recipes_by_resource[resource].append((recipe, rate))

    rates = defaultdict(float)
    clocks = defaultdict(int)
    new_clocks = initial_recipes
    rate_history = set()

    while new_clocks:
        for recipe_name, clock in new_clocks.items():
            clocks[recipe_name] += clock
            for resource, rate in recipes[recipe_name].rates.items():
                new_rate = rates[resource] + rate.scaled_rate(clock)
                if abs(new_rate) > 1e-10:
                    rates[resource] = new_rate
                else:
                    del rates[resource]

        missing_rates = {
            resource: rate
            for resource, rate in rates.items()
            if rate < -1e-10
        }

        # This helps combat graph cycles involving power
        if 'Power' in missing_rates and len(missing_rates) > 1:
            del missing_rates['Power']

        # Cut short any cycles and be satisfied with a terrible approximation
        new_rates = tuple(sorted(missing_rates.keys()))
        if new_rates in rate_history:
            break
        rate_history.add(new_rates)

        new_clocks = defaultdict(float)

        for missing_resource, missing_rate in missing_rates.items():
            # Recipes that could fill the gap
            # Avoid already-present recipes as an approximation that has no cycles
            outputs = [
                (recipe, out_rate)
                for recipe, out_rate in recipes_by_resource[missing_resource]
            ]

            n_outputs = len(outputs)
            if not n_outputs:
                continue
            rate_per = -missing_rate / n_outputs

            # Each output needs to contribute equally to the solution
            # e.g. for output rates of 0.5, 1, 2 /s,
            # and a needed total rate of 32.455 /s,
            # each needs to contribute 10.818 /s
            for recipe, rate in outputs:
                inverse_clock = rate.clock_for_rate(rate_per)
                if inverse_clock >= 0.5:
                    new_clocks[recipe.name] += inverse_clock

    return {
        name: recipes[name] for name in clocks.keys()
    }, clocks


def prune_recipes_old(
    recipes: Dict[str, 'Recipe'], initial_recipes: Dict[str, float],
) -> Tuple[
    Dict[str, 'Recipe'],  # pruned recipes
    Dict[str, float],  # initial clocks
]:
    recipes_by_resource = defaultdict(list)
    for recipe_name, recipe in recipes.items():
        for resource, rate in recipe.rates.items():
            recipes_by_resource[resource].append((recipe, rate))

    initial_rates = defaultdict(float)
    initial_clocks = defaultdict(int)

    def recurse_initial(recipe_name, clock: float):
        initial_clocks[recipe_name] += clock

        for resource, rate in recipes[recipe_name].rates.items():
            initial_rates[resource] += rate.scaled_rate(clock)

            if rate.is_input and initial_rates[resource] < 0:
                outputs = [
                    (recipe, rate)
                    for recipe, rate in recipes_by_resource[resource]
                    if rate.is_output
                ]
                n_outputs = len(outputs)
                rate_per = -initial_rates[resource] / n_outputs

                # Each output needs to contribute equally to the solution
                # e.g. for output rates of 0.5, 1, 2 /s,
                # and a needed total rate of 32.455 /s,
                # each needs to contribute 10.818 /s

                for recipe, rate in outputs:
                    inverse_clock = rate.clock_for_rate(rate_per)
                    if inverse_clock >= 0.5:
                        recurse_initial(
                            recipe.name,
                            math.ceil(inverse_clock),
                        )

    for recipe, clock in initial_recipes.items():
        recurse_initial(recipe, clock)

    pruned_recipes = {
        recipe: recipes[recipe]
        for recipe, clock in initial_clocks.items()
        if clock > 1e-3
    }
    logger.info(f'Kept {len(pruned_recipes)}/{len(recipes)} recipes')
    return pruned_recipes, initial_clocks
