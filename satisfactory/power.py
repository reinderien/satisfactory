import logging
import math
from dataclasses import dataclass
from itertools import chain
from typing import Dict, List, Tuple, TYPE_CHECKING

import numpy as np
from gekko import GEKKO
from gekko.gk_operators import GK_Intermediate, GK_Operators
from gekko.gk_variable import GKVariable

from .logs import logger

if TYPE_CHECKING:
    from .recipe import Recipe

"""
At this point, we have a total percentage for each recipe, but no choice
on allocation of those percentages to a building count, considering
nonlinear power load and the addition of power shards.

For a given maximum building count, there will be an optimal clock
scaling configuration, building allocation and power shard allocation
that minimizes power consumption.
"""


@dataclass
class SolvedRecipe:
    recipe: 'Recipe'
    n: int
    clock_each: int

    @property
    def clock_total(self) -> int:
        return self.clock_each * self.n

    @property
    def power_each(self) -> float:
        return self.recipe.base_power * (self.clock_each / 100) ** 1.6

    @property
    def power_total(self) -> float:
        return self.power_each * self.n

    @property
    def secs_per_output_each(self) -> float:
        return self.secs_per_output_total * self.n

    @property
    def secs_per_output_total(self) -> float:
        rate = self.clock_total / 100 * self.recipe.rates[self.recipe.first_output]
        return 1 / rate

    @property
    def shards_each(self) -> int:
        return max(
            0,
            math.ceil(self.clock_each / 50) - 2,
        )

    @property
    def shards_total(self) -> int:
        return self.shards_each * self.n

    def __str__(self):
        return f'{self.recipe} ×{self.n}'

    def distribute(self) -> Tuple['SolvedRecipe', ...]:
        clock = round(self.clock_total)
        quo, rem = divmod(clock, self.n)
        if not rem:
            return self,

        y = clock - quo * self.n
        assert y == rem
        x = self.n - y

        return (
            SolvedRecipe(self.recipe, n=x, clock_each=quo),
            SolvedRecipe(self.recipe, n=y, clock_each=quo + 1),
        )


class PowerSolver:
    def __init__(
        self,
        recipes: Dict[str, 'Recipe'],
        percentages: Dict[str, float],
        rates: Dict[str, float],
    ):
        recipe_clocks = [
            (recipes[recipe], clock)
            for recipe, clock in percentages.items()
        ]

        # No network; discontinuous problem; respect integer constraints
        APOPT = 1
        self.m = m = GEKKO(remote=False, name='satisfactory_power')
        m.options.solver = APOPT
        m.solver_options = ['minlp_as_nlp 0']

        buildings, self.buildings, self.building_total = self.define_buildings(recipe_clocks)
        self.powers, self.power_total = self.define_power(recipe_clocks, buildings)
        self.clocks = self.define_clocks(recipe_clocks, buildings)

        self.solved: List[SolvedRecipe] = []
        self.recipes, self.rates = recipes, rates

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.m.cleanup()

    def define_buildings(self, recipe_clocks: List[Tuple['Recipe', float]]) -> Tuple[
        np.ndarray,
        Dict[str, GKVariable],
        GK_Intermediate,
    ]:
        building_gen = (
            self.m.Var(
                name=f'{recipe.name} buildings',
                integer=True,
                lb=1,
                value=max(1, clock//100),
            )
            for recipe, clock in recipe_clocks
        )

        # There doesn't seem to be much point in making an Array, since it still
        # translates to individual variables for APM, but whatever
        buildings = self.m.Array(building_gen.__next__, len(recipe_clocks))

        return (
            buildings,
            {
                recipe.name: building
                for (recipe, clock), building in zip(recipe_clocks, buildings)
            },
            self.m.Intermediate(
                buildings.sum(), name='building_total',
            )
        )

    def define_power(
        self,
        recipe_clocks: List[Tuple['Recipe', float]],
        buildings: np.ndarray,
    ) -> Tuple[
        Dict[str, GK_Intermediate],
        GK_Intermediate,
    ]:
        power_gen = (
            self.m.Intermediate(
                building**-0.6 * (clock / 100)**1.6 * recipe.base_power,
                name=f'{recipe.name} power'
            )
            for building, (recipe, clock) in zip(buildings, recipe_clocks)
        )

        powers = self.m.Array(power_gen.__next__, len(recipe_clocks))

        return (
            {
                recipe.name: power
                for (recipe, _), power in zip(recipe_clocks, powers)
            },
            self.m.Intermediate(
                powers.sum(), name=f'power_total',
            ),
        )

    def define_clocks(
        self,
        recipe_clocks: List[Tuple['Recipe', float]],
        buildings: np.ndarray,
    ) -> Dict[str, GK_Intermediate]:
        clock_gen = (
            self.m.Intermediate(
                clock/building,
                name=f'{recipe.name} clock each',
            )
            for building, (recipe, clock) in zip(buildings, recipe_clocks)
        )

        clocks: np.ndarray = self.m.Array(clock_gen.__next__, len(recipe_clocks))

        for clock in clocks:
            self.m.Equation(clock <= 250)

        return {
            recipe.name: clock
            for (recipe, _), clock in zip(recipe_clocks, clocks)
        }

    def constraints(self, *args: GK_Operators):
        self.m.Equations(args)

    def maximize(self, expr: GK_Operators):
        self.m.Maximize(expr)

    def minimize(self, expr: GK_Operators):
        self.m.Minimize(expr)

    def solve(self):
        self.m.solve(disp=logger.level <= logging.DEBUG)

        solved = (
            SolvedRecipe(
                self.recipes[recipe],
                round(building.value[0]),
                self.clocks[recipe].value[0],
            ).distribute()
            for recipe, building in self.buildings.items()
        )

        self.solved.extend(chain.from_iterable(solved))

    @property
    def total_shards(self) -> int:
        return sum(s.shards_total for s in self.solved)

    def print(self):
        print(
            f'{"Recipe":40} '
            f'{"Clock":5} '
            f'{"n":>2} '

            f'{"P (MW)":>6} {"tot":>6} '
            f'{"shards":>6} {"tot":>3} '
            f'{"s/out":>5} {"tot":>4} {"s/extra":>7}'
        )

        for s in self.solved:
            print(
                f'{s.recipe.name:40} '
                f'{s.clock_each:>5.0f} '
                f'{s.n:>2} '

                f'{s.power_each / 1e6:>6.2f} {s.power_total / 1e6:>6.2f} '
                f'{s.shards_each:>6} {s.shards_total:>3} '
                f'{s.secs_per_output_each:>5.1f} {s.secs_per_output_total:>4.1f} '
                f'{s.recipe.secs_per_extra(self.rates):>7}'
            )

        print(
            f'{"Total":40} '
            f'{"":5} {round(self.building_total.value[0]):>2} '
            f'{"":6} {self.power_total.value[0] / 1e6:>6.2f} '
            f'{"":6} {self.total_shards:>3}'
        )
