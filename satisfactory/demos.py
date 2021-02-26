from .power import PowerSolver
from .rates import setup_linprog, solve_linprog, get_clocks, get_rates
from .recipe import load_recipes


def hungry_plating():
    print('Factory to produce smart plating at at least 1 every 10s, '
          'minimizing the number of buildings, which implies huge power '
          'consumption and shards')
    recipes = load_recipes(tier_before=3)
    problem = setup_linprog(recipes,
                            min_rates={'Smart Plating': 0.1})
    solve_linprog(problem)

    with PowerSolver(recipes,
                     percentages=dict(get_clocks(problem)),
                     rates=dict(get_rates(problem))) as power:
        power.constraints(power.building_total <= 50,
                          power.shard_total <= 24,  # limiting factor
                          power.power_total <= 500e6)
        power.minimize(power.building_total)
        power.solve()
        power.print()


def fast_rotors():
    print('Factory to produce as many rotors as possible, limited by power and '
          'building count, via approximate scaling')
    recipes = load_recipes(tier_before=3)
    problem = setup_linprog(recipes,
                            fixed_clocks={'Rotor': 100})
    solve_linprog(problem)

    with PowerSolver(recipes,
                     percentages=dict(get_clocks(problem)),
                     rates=dict(get_rates(problem)),
                     scale_clock=True) as power:
        power.constraints(power.building_total <= 50,
                          power.power_total <= 100e6)
        power.maximize(power.clock_totals['Rotor'])
        power.solve()
        power.print()
