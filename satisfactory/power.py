import enum
import logging
import math
from dataclasses import dataclass
from enum import Enum
from itertools import chain
from numbers import Number
from typing import Dict, Iterable, List, Tuple, TYPE_CHECKING

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


APOPT = 1


@dataclass
class SolvedRecipe:
    recipe: 'Recipe'
    n: int
    clock_total: int

    @property
    def clock_each(self) -> int:
        return self.clock_total // self.n

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

        x = self.n - rem
        y = rem

        return (
            SolvedRecipe(self.recipe, n=x, clock_total=x*quo),
            SolvedRecipe(self.recipe, n=y, clock_total=y*(quo + 1)),
        )


def pure_sum(series: Iterable[Number]) -> Number:
    series = iter(series)
    total = next(series)
    for x in series:
        total += x
    return total


@enum.unique
class ShardMode(Enum):
    NONE = enum.auto()
    # See https://apmonitor.com/wiki/index.php/Main/Objects
    MPEC = enum.auto()
    # See https://apmonitor.com/wiki/index.php/Main/Objects
    BINARY = enum.auto()

    @property
    def method_numeral(self) -> int:
        if self == self.MPEC:
            return 2
        if self == self.BINARY:
            return 3
        raise NotImplementedError()


class PowerSolver:
    def __init__(
        self,
        recipes: Dict[str, 'Recipe'],
        percentages: Dict[str, float],
        rates: Dict[str, float],
        scale_clock: bool = False,
        shard_mode: ShardMode = ShardMode.BINARY,
    ):
        self.solved: List[SolvedRecipe] = []
        self.recipes, self.rates, self.shard_mode = recipes, rates, shard_mode

        recipe_clocks = [
            (recipes[recipe], clock)
            for recipe, clock in percentages.items()
        ]

        # No network; discontinuous problem; respect integer constraints
        self.m = m = GEKKO(remote=False, name='satisfactory_power')
        m.options.solver = APOPT
        m.solver_options = [
            'minlp_as_nlp 0',
            'minlp_gap_tol 1e-3',
            'minlp_integer_tol 1e-3',
        ]

        if scale_clock:
            logger.warning('Scaling enabled; inexact solution likely')
            self.clock_scale = m.Var(
                name='clock_scale', value=1,
            )
            m.Equation(self.clock_scale > 0)
        else:
            self.clock_scale = m.Const(1, 'scale')

        self.buildings, self.building_total = self.define_buildings(recipe_clocks)
        self.powers, self.power_total = self.define_power(recipe_clocks)
        self.clocks_each, self.clock_totals = self.define_clocks(recipe_clocks)

        if shard_mode != ShardMode.NONE:
            self.shards_each, self.shard_totals, self.shard_total = self.define_shards()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.m.cleanup()

    def define_buildings(self, recipe_clocks: List[Tuple['Recipe', float]]) -> Tuple[
        Dict[str, GKVariable],
        GK_Intermediate,
    ]:
        buildings = [
            self.m.Var(
                name=f'{recipe.name} buildings',
                integer=True,
                lb=1,
                value=max(1, clock//100),
            )
            for recipe, clock in recipe_clocks
        ]

        return (
            {
                recipe.name: building
                for (recipe, _), building in zip(recipe_clocks, buildings)
            },
            self.m.Intermediate(
                pure_sum(buildings), name='building_total',
            )
        )

    def define_power(
        self,
        recipe_clocks: List[Tuple['Recipe', float]],
    ) -> Tuple[
        Dict[str, GK_Intermediate],
        GK_Intermediate,
    ]:
        powers = [
            self.m.Intermediate(
                self.buildings[recipe.name]**-0.6
                * (clock * self.clock_scale / 100)**1.6
                * recipe.base_power,
                name=f'{recipe.name} power'
            )
            for recipe, clock in recipe_clocks
        ]

        return (
            {
                recipe.name: power
                for (recipe, _), power in zip(recipe_clocks, powers)
            },
            self.m.Intermediate(
                pure_sum(powers), name=f'power_total',
            ),
        )

    def define_clocks(
        self,
        recipe_clocks: List[Tuple['Recipe', float]],
    ) -> Tuple[
        Dict[str, GK_Intermediate],
        Dict[str, GK_Intermediate],
    ]:
        clock_totals = [
            self.m.Intermediate(
                clock * self.clock_scale,
                name=f'{recipe.name} clock total',
            )
            for recipe, clock in recipe_clocks
        ]

        clocks = [
            self.m.Intermediate(
                clock / self.buildings[recipe.name],
                name=f'{recipe.name} clock each',
            )
            for clock, (recipe, _) in zip(clock_totals, recipe_clocks)
        ]

        for clock in clocks:
            self.m.Equation(clock <= 250)

        return (
            {
                recipe.name: clock
                for (recipe, _), clock in zip(recipe_clocks, clocks)
            },
            {
                recipe.name: clock
                for (recipe, _), clock in zip(recipe_clocks, clock_totals)
            },
        )

    def define_shards(self) -> Tuple[
        Dict[str, GKVariable],
        Dict[str, GK_Intermediate],
        GK_Intermediate,
    ]:
        shards_each = {}
        shards_total = {}
        zero = self.m.Param(name='zero', value=0)
        f_max = getattr(self.m, f'max{self.shard_mode.method_numeral}')

        for recipe, clock in self.clocks_each.items():
            # e.g.
            # max(0, 120/50 - 2) = 0.4
            # 0.4 <= 1 < 1.4
            # max(0, 100/50 - 2) = 0
            # 0 <= 0 < 1
            shards_cont = self.m.Var(name=f'{recipe} shards cont')
            shards_pos = f_max(zero, shards_cont)

            shards = shards_each[recipe] = self.m.Var(
                name=f'{recipe} shards each',
                integer=True,
            )
            shards_total[recipe] = self.m.Intermediate(
                name=f'{recipe} shards total',
                equation=shards * self.buildings[recipe],
            )

            # 100 corresponds to 0, so 100.5 corresponds to 0.01
            # This 0.5 offset is between two integer percentage points so there
            # should be no boundary problems
            self.m.Equations((
                shards_cont == clock/50 - 2,
                shards_pos - 0.01 <= shards,
                shards < shards_pos + 0.99,
            ))

        return (
            shards_each,
            shards_total,
            self.m.Intermediate(
                pure_sum(shards_total.values()), name=f'shard_total',
            )
        )

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
                round(self.clock_totals[recipe].value[0]),
            ).distribute()
            for recipe, building in self.buildings.items()
        )

        self.solved.extend(chain.from_iterable(solved))

        self.verify_shards()

    def verify_shards(self):
        if self.shard_mode == ShardMode.NONE:
            return

        if not any(
            'shard_total' in eq.value
            for eq in self.m._equations
        ):
            logger.warning('No shard constraints; switch to ShardMode.NONE')

        has_error = False
        act_total = self.actual_shards
        est_total, = self.shard_total
        if act_total != round(est_total):
            logger.error(
                f'The total shard solution is wrong: '
                f'approx {est_total} != actual {act_total}'
            )
            has_error = True

        for solved in self.solved:
            name = solved.recipe.name
            act = solved.shards_each
            est, = self.shards_each[name]
            if act != round(est):
                logger.error(
                    f'The shard solution for "{name}" is wrong: '
                    f'returned {est} != actual {act}'
                )
                has_error = True

        if has_error:
            if self.shard_mode == ShardMode.MPEC:
                logger.info(f'Consider ShardMode.BINARY instead')
        elif self.shard_mode == ShardMode.BINARY and self.actual_shards > 0:
            logger.warning('ShardMode.BINARY risks this solution being non-optimal')

    @property
    def clock_scale_value(self) -> float:
        v = self.clock_scale.value
        if isinstance(self.clock_scale, GKVariable):
            return v[0]
        return v

    @property
    def actual_power(self) -> float:
        return sum(s.power_total for s in self.solved)

    @property
    def actual_shards(self) -> int:
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
                f'{s.recipe.secs_per_extra(self.rates, self.clock_scale_value):>7}'
            )

        if self.shard_mode == ShardMode.NONE:
            shards = '   '
        else:
            shards = f'{self.shard_total.value[0]:.0f}'

        print(
            f'{"Total approx":40} '
            f'{"":5} {"":>2} '
            f'{"":6} {self.power_total.value[0] / 1e6:>6.2f} '
            f'{"":6} {shards:>3}\n'
            
            f'{"Total actual":40} '
            f'{"":5} {round(self.building_total.value[0]):>2} '
            f'{"":6} {self.actual_power / 1e6:>6.2f} '
            f'{"":6} {self.actual_shards:>3}'
        )
