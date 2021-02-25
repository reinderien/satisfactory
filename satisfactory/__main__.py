from .logs import logger
from .power import PowerSolver
from .rates import setup_linprog, solve_linprog, get_clocks, get_rates
from .recipe import load_recipes


def main():
    recipes = load_recipes(tier_before=3)
    logger.info(f'{len(recipes)} recipes loaded.')

    logger.info('Rate-pinning stage...')
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

    logger.info('Power stage...')
    with PowerSolver(
        recipes, percentages, rates,
    ) as power:
        power.constraints(
            power.building_total <= 50,
            power.power_total <= 100e6,
        )
        power.minimize(power.power_total)
        power.solve()
        power.print()


main()
