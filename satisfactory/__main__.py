from .logs import logger
from .recipe import load_recipes
from .rates import setup_linprog, get_clocks, solve_linprog, get_rates
from .power import Solution, PowerObjective


def main():
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
    soln = Solution.solve(
        recipes, percentages, rates,
        minimize=PowerObjective.POWER,
        max_buildings=50,
        max_power=100e6,
    )
    soln.print()


main()
