from contextlib import contextmanager
from sys import stderr
from typing import Tuple, Dict
from gekko.gk_operators import GK_Operators

from .power import PowerSolver, ShardMode
from .recipe import load_recipes, graph_recipes


TIERS_TO_5 = {
    'Tier 0',
    'Tier 1',
    'Tier 2',
    'Tier 3',
    'Tier 4',
    'M.A.M.',
}


def graph_db():
    recipes = load_recipes(TIERS_TO_5)
    graph_recipes(recipes)


def multi_outputs():
    print('Factory to produce modular frames, rotors and smart plating, '
          'limited by building count, minimizing power')
    recipes = load_recipes({
        'Tier 0', 'Tier 2',
    })

    with PowerSolver(recipes,
                     initial_recipes={
                         'Modular Frame': 100,
                         'Rotor': 100,
                         'Smart Plating': 100,
                     }) as sol:
        sol.constraints(
            sol.clock_totals['Modular Frame'] >= 100,
            sol.clock_totals['Rotor'] >= 100,
            sol.clock_totals['Smart Plating'] >= 100,
            sol.building_total <= 50,
        )
        sol.minimize(sol.power_total)
        sol.solve()
        sol.print()
        sol.graph()


def hungry_plating():
    print('Factory to produce smart plating at at least 1 every 10s, '
          'minimizing the number of buildings, which implies huge power '
          'consumption and shards')
    recipes = load_recipes(TIERS_TO_5)

    with PowerSolver(recipes,
                     initial_recipes={'Smart Plating': 100},
                     # We care about shards, so this can't be NONE; but also
                     # BINARY produces a non-optimal solution
                     shard_mode=ShardMode.MPEC) as power:
        power.constraints(power.building_total <= 50,
                          power.shard_total <= 24,  # limiting factor
                          power.power_total <= 500e6,
                          power.rates['Smart Plating'] >= 0.1)
        power.minimize(power.building_total)
        power.solve()
        power.print()
        power.graph()


def fast_rotors():
    print('Factory to produce as many rotors as possible, limited by power and '
          'building count')
    recipes = load_recipes(TIERS_TO_5)

    with PowerSolver(recipes,
                     initial_recipes={'Rotor': 100}) as power:
        power.constraints(power.building_total <= 25,
                          power.power_total <= 100e6)
        power.maximize(power.clock_totals['Rotor'])
        power.solve()
        power.print()
        power.graph()


def big_tier_4():
    print('Factory to produce a collection of tier-4 resources, limited by '
          'power and building count')

    outputs = {
        'A.I. Limiter': 25,
        'Automated Wiring': 25,
        'Motor': 25,
        'Quartz Crystal': 25,
        'Smart Plating': 25,
        'Versatile Framework': 25,
    }
    output_names = tuple(outputs.keys())

    recipes = load_recipes(TIERS_TO_5)

    with PowerSolver(recipes,
                     initial_recipes=outputs) as power:
        power.constraints(
            power.building_total <= 100,
            power.power_total <= 375e6,
            *(
                power.clock_totals[output_names[i]] ==
                power.clock_totals[output_names[i + 1]]
                for i in range(len(outputs) - 1)
            ),
        )
        power.maximize(power.clock_totals['Automated Wiring'])
        power.solve()
        power.print()
        power.graph()


def current():
    print('Current gameplay based on fixed mine recipes')

    def constraints(power) -> Tuple[GK_Operators, ...]:
        return (
            power.building_total <= 100,
            power.power_total <= 300e6,
            # power.shard_total <= 94,
            power.buildings['Iron Ore from Miner Mk.1 on Pure node'] +
            power.buildings['Iron Ore from Miner Mk.2 on Pure node'] == 3,
            power.buildings['Copper Ore from Miner Mk.1 on Pure node'] +
            power.buildings['Copper Ore from Miner Mk.2 on Pure node'] == 1,
            power.buildings['Caterium Ore from Miner Mk.1 on Pure node'] +
            power.buildings['Caterium Ore from Miner Mk.2 on Pure node'] == 1,
        )

    recipes = load_recipes(TIERS_TO_5)

    min_rates = {
        'A.I. Limiter': 0.1,
        'Encased Industrial Beam': 0.1,
        'Motor': 0.1,
        'Versatile Framework': 0.1,
    }

    @contextmanager
    def solve(**kwargs):
        with PowerSolver(recipes,
                         initial_recipes={
                            name: 100
                            for name, rate in min_rates.items()
                         },
                         **kwargs) as power:
            power.constraints(*constraints(power))
            yield power
            power.solve()

    def round_clocks(power) -> Dict[str, int]:
        return {solved.recipe.name: round(solved.clock_total)
                for solved in power.solved}

    fixed_clocks = None

    best_rate = 0
    best_soln = None

    for i in range(3):
        print(f'\nIteration {i}', file=stderr)
        stderr.flush()
        with solve(
            # shard_mode=ShardMode.BINARY,
        ) as approx:
            approx.maximize(approx.clock_totals['Versatile Framework'])

        min_rates = None
        print('\nExact, minimize buildings', file=stderr)
        stderr.flush()
        with solve() as exact:
            exact.minimize(exact.power_total)

        delay = next(
            s for s in exact.solved if s.recipe.name == 'Versatile Framework'
        ).secs_per_output_total
        print(f's/Versatile Framework: {delay}', file=stderr)

        rate = 1/delay
        if best_rate < rate:
            best_rate = rate
            best_soln = exact

        fixed_clocks = round_clocks(exact)

    best_soln.print()
    best_soln.graph()
