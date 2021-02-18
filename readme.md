Oh no, not this again
---------------------

[Yes](https://github.com/reinderien/factorio),
[this](https://github.com/reinderien/terraria-npcs)
[again](https://github.com/reinderien/blockhood). Me doing dumb things with 
video games.

This time it's [Satisfactory](https://www.satisfactorygame.com). It's a very 
promising game in early-ish beta that is basically a modern version of
[Infinifactory](https://store.steampowered.com/app/300570/Infinifactory)
with, so far, somewhat less plot and significantly better graphics.

Database
--------

Before analysing, we need something to analyse: so let's use the
[MediaWiki API](https://www.mediawiki.org/wiki/API:Main_page) to download data
about the game from its
[Gamepedia site](https://satisfactory.gamepedia.com).
The easiest path here is to

- download and parse a template that links to all components, ignoring
  components above a specified tier;
- download and parse all component pages for their recipe data; and
- download and parse data for any ores needed by those components.

The MediaWiki API allows this to be done in only three requests, which is nice;
and the whole thing is completed extremely quickly - so quickly, in fact, that
I haven't found it necessary to cache anything locally.

Analysis
--------

So let's analyse the data. I'll not make the same mistake again in pursuing an
optimization package that's missing Mixed-Integer Linear Programming (MIP).
[GLPK](https://www.gnu.org/software/glpk) has it, and I've used it before -
indirectly - through different bindings. This time, since I'm using Python, I'll
use [swiglpk](https://github.com/biosustain/swiglpk), the only set of Python
bindings that decided to actually f---ing install. This is a paper-thin wrapper
around the binary library and that's fine.

GLPK 5.0 is a new version of an old (2000) and well-established library. The
one-based indexing is dumb, and parts of the documentation are a little awkward,
but the algorithmic accuracy and performance are excellent.

I want to ask the solver, e.g. "for assemblers running at full tilt, one each
to produce rotors, modular frames and smart plating, tell me the minimum factory
configuration needed to supply them". Translating this into GLPK speak,

- The _structural variables_ are in a vector, each element being a count of a
  given recipe; a.k.a. the number of buildings running that recipe
- The _auxiliary variables_ are in another vector, each element being the rate
  of production of a given resource (consumption is negative).
- The _bounds_ are the minimum and maximum for each of the structural and
  auxiliary variables. In the final answer, no resource rate can be negative, or 
  else the factory will run out. The bounds are also used to pre-set a desired 
  rate of production for a given resource or count of a given recipe.
- The _objective function_ is a scalar number, a cost to minimize, in our case
  the building count (constructors, assemblers, smelters and miners)
- The _objective coefficients_ represent how expensive each recipe is to us.
  Currently this doesn't matter so we set every one to 1.
- The _constraint coefficient matrix_ is a grid of all resource rates for all
  recipes.

The vast majority of the code interacting with GLPK is to tell it about all of
the above. Once we have, getting to a solution is easy:

- Run the [Dantzig Simplex Algorithm](https://en.wikipedia.org/wiki/Simplex_algorithm)
  to get what GLPK calls a "basis" - an initial solution that allows for
  fractional recipe counts
- To reduce the fractional recipe counts to whole-integer recipe counts, run the
  [branch-and-bound](https://en.wikipedia.org/wiki/Branch_and_bound)
  MIP solver.
  
Output
------

Currently - for a maximum tech tier of 2, and asking for the three output
recipes mentioned above - this produces:

```
Fetching recipe data...

GLPK Simplex Optimizer 5.0
23 rows, 28 columns, 51 non-zeros
      0: obj =   3.000000000e+00 inf =   2.283e+00 (3)
      6: obj =   1.917500000e+01 inf =   0.000e+00 (0)
OPTIMAL LP SOLUTION FOUND

GLPK Integer Optimizer 5.0
23 rows, 28 columns, 51 non-zeros
28 integer variables, none of which are binary
Integer optimization begins...
Long-step dual simplex will be used
+     6: mip =     not found yet >=              -inf        (1; 0)
Solution found by heuristic: 22
+    10: mip =   2.200000000e+01 >=     tree is empty   0.0% (0; 3)
INTEGER OPTIMAL SOLUTION FOUND
Writing MIP solution to '/tmp/tmps17rs8ja'...
Problem:    satisfactory
Rows:       23
Columns:    28 (28 integer, 0 binary)
Non-zeros:  51
Status:     INTEGER OPTIMAL
Objective:  n_buildings = 22 (MINimum)

   No.   Row name        Activity     Lower bound   Upper bound
------ ------------    ------------- ------------- -------------
     1 Alien Carapace
                                   0             0               
     2 Alien Organs                0             0               
     3 Biomass                     0             0               
     4 Cable                       0             0               
     5 Concrete                    0             0               
     6 Copper Ingot                0             0               
     7 Copper Ore                  0             0               
     8 Copper Sheet                0             0               
     9 Iron Ingot               0.25             0               
    10 Iron Ore                  0.5             0               
    11 Iron Plate           0.166667             0               
    12 Iron Rod                 0.05             0               
    13 Leaves                      0             0               
    14 Limestone                   0             0               
    15 Modular Frame
                           0.0333333             0               
    16 Mycelia                     0             0               
    17 Reinforced Iron Plate
                                   0             0               
    18 Rotor               0.0333333             0               
    19 Screw                       0             0               
    20 Smart Plating
                           0.0333333             0               
    21 Solid Biofuel
                                   0             0               
    22 Wire                        0             0               
    23 Wood                        0             0               

   No. Column name       Activity     Lower bound   Upper bound
------ ------------    ------------- ------------- -------------
     1 Biomass (Leaves)
                    *              0             0               
     2 Biomass (Wood)
                    *              0             0               
     3 Biomass (Mycelia)
                    *              0             0               
     4 Biomass (Alien Carapace)
                    *              0             0               
     5 Biomass (Alien Organs)
                    *              0             0               
     6 Cable        *              0             0               
     7 Concrete     *              0             0               
     8 Copper Ingot *              0             0               
     9 Copper Sheet *              0             0               
    10 Iron Ingot   *              5             0               
    11 Iron Plate   *              2             0               
    12 Iron Rod     *              5             0               
    13 Modular Frame
                    *              1             1             = 
    14 Reinforced Iron Plate
                    *              1             0               
    15 Rotor        *              1             1             = 
    16 Screw        *              4             0               
    17 Smart Plating
                    *              1             1             = 
    18 Solid Biofuel
                    *              0             0               
    19 Wire         *              0             0               
    20 Copper Ore from Miner Mk. 1 on Impure node
                    *              0             0               
    21 Copper Ore from Miner Mk. 1 on Normal node
                    *              0             0               
    22 Copper Ore from Miner Mk. 1 on Pure node
                    *              0             0               
    23 Iron Ore from Miner Mk. 1 on Impure node
                    *              0             0               
    24 Iron Ore from Miner Mk. 1 on Normal node
                    *              1             0               
    25 Iron Ore from Miner Mk. 1 on Pure node
                    *              1             0               
    26 Limestone from Miner Mk. 1 on Impure node
                    *              0             0               
    27 Limestone from Miner Mk. 1 on Normal node
                    *              0             0               
    28 Limestone from Miner Mk. 1 on Pure node
                    *              0             0               

Integer feasibility conditions:

KKT.PE: max.abs.err = 0.00e+00 on row 0
        max.rel.err = 0.00e+00 on row 0
        High quality

KKT.PB: max.abs.err = 2.22e-16 on row 19
        max.rel.err = 2.22e-16 on row 19
        High quality

End of output
```

Interpreting this:

- Any variable with an `=` to the right is a fixed recipe I'm asking for at the
  output
- _Activity_ for the row section is the resource rate per second
- _Activity_ for the column section is the recipe (building) count
