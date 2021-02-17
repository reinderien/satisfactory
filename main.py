#!/usr/bin/env python3

import re
from dataclasses import dataclass
from itertools import count, chain
from sys import stderr
from typing import Iterable, List, Dict, ClassVar, Pattern, Collection

from requests import Session


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
    def from_page(cls, page: str) -> Iterable['Recipe']:
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


def linprog():
    import swiglpk as pk

    lp = pk.glp_create_prob()
    pk.glp_set_prob_name(lp, 'satisfactory')

    '''
    n is the number of variables
    m is the number of constraints
    x is an n-vector, auxiliary variables
    x[m+] is an n-vector, structural variables
    z is the scalar cost/objective function
    c is an n-vector, objective coefficients
    a is an m*n constraint coefficient matrix
    
    
    minimize
    z=c[1]*x[m+1] + c[2]*x[m+2] +...+c[n]*x[m+n] + c[0]
    
    subject to linear constraints
    x[1] = a[11]*x[m+1] + a[12]*x[m+2] + ... + a[1n]*x[m+n]
    
    and bounds of variables
    l1 ≤ x1 ≤ u1
    '''



def main():
    linprog()

    with Session() as session:
        component_text, = get_api(session, titles='Template:ItemNav')
        component_names = parse_template(component_text, tier_before=3)
        component_pages = get_api(
            session,
            titles='|'.join(component_names),
            # rvsection=2,  # does not work for Biomass
        )

        recipes = list(chain.from_iterable(
            Recipe.from_page(page)
            for page in component_pages
        ))
        recipes.extend(fill_missing(session, recipes))



main()
