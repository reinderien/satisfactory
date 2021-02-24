import enum
import logging
import math
from dataclasses import dataclass
from enum import Enum
from itertools import chain
from typing import Collection, Dict, List, Tuple, Optional, TYPE_CHECKING

from gekko import GEKKO

from .logs import logger

if TYPE_CHECKING:
    from .recipe import Recipe


@enum.unique
class PowerObjective(Enum):
    POWER = 'power'
    BUILDINGS = 'buildings'


@dataclass
class SolvedRecipe:
    recipe: 'Recipe'
    n: int
    clock_total: int

    @property
    def clock_each(self) -> float:
        return self.clock_total / self.n

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
        quo, rem = divmod(self.clock_total, self.n)
        if not rem:
            return self,

        y = self.clock_total - quo * self.n
        x = self.n - y

        return (
            SolvedRecipe(self.recipe, x, x * quo),
            SolvedRecipe(self.recipe, y, y * (quo + 1)),
        )


@dataclass
class Solution:
    recipes: List[SolvedRecipe]
    rates: Dict[str, float]

    @classmethod
    def solve(
            cls,
            recipes: Dict[str, 'Recipe'],
            percentages: Dict[str, float],
            rates: Dict[str, float],
            minimize: PowerObjective,
            max_buildings: Optional[int] = None,
            max_power: Optional[float] = None,
    ) -> 'Solution':
        """
        At this point, we have a total percentage for each recipe, but no choice
        on allocation of those percentages to a building count, considering
        nonlinear power load and the addition of power shards.

        For a given maximum building count, there will be an optimal clock
        scaling configuration, building allocation and power shard allocation
        that minimizes power consumption.
        """

        rate_items: Collection[Tuple['Recipe', float]] = [
            (recipes[recipe], rate)
            for recipe, rate in percentages.items()
        ]

        # No network; discontinuous problem; respect integer constraints
        APOPT = 1
        m = GEKKO(remote=False, name='satisfactory_power')
        m.options.solver = APOPT
        m.solver_options = ['minlp_as_nlp 0']

        buildings = [
            m.Var(integer=True, name=recipe.name, lb=1)
            for recipe, rate in rate_items
        ]
        building_total = sum(buildings)

        powers = [
            build_var ** -0.6 * (clock / 100) ** 1.6 * recipe.base_power
            for build_var, (recipe, clock) in zip(buildings, rate_items)
        ]
        power_total = sum(powers)

        for (recipe, clock), building in zip(rate_items, buildings):
            m.Equation(clock / building <= 250)

        limit_strs = []
        if max_buildings is None:
            if minimize == PowerObjective.POWER:
                raise ValueError('Min power requires a building limit')
        else:
            m.Equation(building_total <= max_buildings)
            limit_strs.append(f'{max_buildings} buildings')

        if max_power is not None:
            m.Equation(power_total <= max_power)
            limit_strs.append(f'{max_power / 1e6:.0f} MW power')

        if minimize == PowerObjective.BUILDINGS:
            m.Minimize(building_total)
        elif minimize == PowerObjective.POWER:
            m.Minimize(power_total)

        msg = f'Minimizing {minimize.value}'
        if limit_strs:
            msg += ' for at most ' + ' and '.join(limit_strs)
        logger.info(msg)

        m.solve(disp=logger.level <= logging.DEBUG)

        solved = (
            SolvedRecipe(
                recipe=recipe,
                n=int(building.value[0]),
                clock_total=int(clock),
            ).distribute()
            for building, (recipe, clock) in zip(buildings, rate_items)
        )
        return cls(
            recipes=list(chain.from_iterable(solved)),
            rates=rates,
        )

    @property
    def total_buildings(self) -> int:
        return sum(s.n for s in self.recipes)

    @property
    def total_power(self) -> float:
        return sum(s.power_total for s in self.recipes)

    @property
    def total_shards(self) -> int:
        return sum(s.shards_total for s in self.recipes)

    def print(self):
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

                f'{s.power_each / 1e6:>6.2f} {s.power_total / 1e6:>6.2f} '
                f'{s.shards_each:>6} {s.shards_total:>3} '
                f'{s.secs_per_output_each:>5.1f} {s.secs_per_output_total:>4.1f} '
                f'{s.recipe.secs_per_extra(self.rates):>7}'
            )

        print(
            f'{"Total":40} '
            f'{"":5} {self.total_buildings:>2} '
            f'{"":6} {self.total_power / 1e6:>6.2f} '
            f'{"":6} {self.total_shards:>3}'
        )
