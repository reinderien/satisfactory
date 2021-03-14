from contextlib import contextmanager

from .power import PowerSolver, ShardMode
from .rates import setup_linprog, solve_linprog, get_clocks, get_rates
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
    recipes = load_recipes(TIERS_TO_5)
    problem = setup_linprog(
        recipes,
        fixed_clocks={
            'Modular Frame': 100,
            'Rotor': 100,
            'Smart Plating': 100,
        }
    )
    solve_linprog(problem)

    with PowerSolver(recipes,
                     percentages=dict(get_clocks(problem)),
                     rates=dict(get_rates(problem))) as sol:
        sol.constraints(sol.building_total <= 50)
        sol.minimize(sol.power_total)
        sol.solve()
        sol.print()
        sol.graph()


def hungry_plating():
    print('Factory to produce smart plating at at least 1 every 10s, '
          'minimizing the number of buildings, which implies huge power '
          'consumption and shards')
    recipes = load_recipes(TIERS_TO_5)
    problem = setup_linprog(recipes,
                            min_rates={'Smart Plating': 0.1})
    solve_linprog(problem)

    with PowerSolver(recipes,
                     percentages=dict(get_clocks(problem)),
                     rates=dict(get_rates(problem)),
                     # We care about shards, so this can't be NONE; but also
                     # BINARY produces a non-optimal solution
                     shard_mode=ShardMode.MPEC) as power:
        power.constraints(power.building_total <= 50,
                          power.shard_total <= 24,  # limiting factor
                          power.power_total <= 500e6)
        power.minimize(power.building_total)
        power.solve()
        power.print()
        power.graph()


def fast_rotors():
    print('Factory to produce as many rotors as possible, limited by power and '
          'building count, via two-stage approximate scaling')
    recipes = load_recipes(TIERS_TO_5)
    problem = setup_linprog(recipes,
                            fixed_clocks={'Rotor': 100})
    solve_linprog(problem)

    with PowerSolver(recipes,
                     percentages=dict(get_clocks(problem)),
                     rates=dict(get_rates(problem)),
                     scale_clock=True) as approx:
        approx.constraints(approx.building_total <= 25,
                           approx.power_total <= 100e6)
        approx.maximize(approx.clock_totals['Rotor'])
        approx.solve()

    print('\nRefined:')
    problem = setup_linprog(
        recipes,
        fixed_clocks={
            'Rotor': round(approx.clock_totals['Rotor'][0]),
        }
    )
    solve_linprog(problem)

    with PowerSolver(recipes,
                     percentages=dict(get_clocks(problem)),
                     rates=dict(get_rates(problem))) as exact:
        exact.constraints(exact.building_total <= 25)
        exact.minimize(exact.power_total)
        exact.solve()
        exact.print()
        exact.graph()


def big_tier_4():
    print('Factory to produce a collection of tier-4 resources, limited by '
          'power and building count')

    recipes = load_recipes(TIERS_TO_5)
    problem = setup_linprog(recipes,
                            min_clocks={
                                'A.I. Limiter': 100,
                                'Automated Wiring': 100,
                                'Motor': 100,
                                'Quartz Crystal': 100,
                                'Smart Plating': 100,
                                'Versatile Framework': 100,
                            })
    solve_linprog(problem)

    with PowerSolver(recipes,
                     percentages=dict(get_clocks(problem)),
                     rates=dict(get_rates(problem)),
                     scale_clock=True) as approx:
        approx.constraints(approx.building_total <= 100,
                           approx.power_total <= 375e6)
        approx.maximize(approx.clock_totals['Automated Wiring'])
        approx.solve()

    print('\nRefined:')
    problem = setup_linprog(
        recipes,
        min_clocks={solved.recipe.name: round(solved.clock_total)
                    for solved in approx.solved})
    solve_linprog(problem)

    with PowerSolver(recipes,
                     percentages=dict(get_clocks(problem)),
                     rates=dict(get_rates(problem))) as exact:
        exact.constraints(exact.building_total <= 100)
        exact.minimize(exact.power_total)
        exact.solve()
        exact.print()
        exact.graph()


def current():
    print('Current gameplay based on fixed mine recipes')

    def constraints(power):
        return (
            power.building_total <= 100,
            power.power_total <= 300e6,
            # power.shard_total <= 94,
            power.buildings['Iron Ore from Miner Mk.2 on Pure node'] == 3,
            power.buildings['Copper Ore from Miner Mk.2 on Pure node'] == 1,
            power.buildings['Caterium Ore from Miner Mk.2 on Pure node'] == 1,
        )

    recipes = load_recipes(TIERS_TO_5)
    @contextmanager
    def solve(problem, **kwargs):
        with PowerSolver(recipes,
                         percentages=dict(get_clocks(problem)),
                         rates=dict(get_rates(problem)),
                         **kwargs) as power:
            power.constraints(*constraints(power))
            yield power
            power.solve()

    problem = setup_linprog(recipes,
                            min_rates={
                                'A.I. Limiter': 0.1,
                                'Encased Industrial Beam': 0.1,
                                'Heavy Modular Frame': 0.1,
                                'Motor': 0.1,
                                'Versatile Framework': 0.1,
                            })
    solve_linprog(problem)

    for iteration in range(2):

        with PowerSolver(recipes,
                         percentages=dict(get_clocks(problem)),
                         rates=dict(get_rates(problem)),
                         # shard_mode=ShardMode.BINARY,
                         scale_clock=True) as approx:

            constraints =

            approx.constraints(*constraints)
            approx.maximize(approx.clock_totals['Versatile Framework'])
            approx.solve()
            approx.print()

        problem = setup_linprog(
            recipes,
            min_clocks={solved.recipe.name: round(solved.clock_total)
                        for solved in approx.solved})
        solve_linprog(problem)

        with PowerSolver(recipes,
                         percentages=dict(get_clocks(problem)),
                         rates=dict(get_rates(problem))) as exact:
            exact.constraints(*constraints)
            exact.minimize(exact.building_total)
            exact.solve()
            exact.print()

        approx = exact

    exact.graph()
