Oh no, not this again
---------------------

[Yes](https://github.com/reinderien/factorio),
[this](https://github.com/reinderien/terraria-npcs)
[again](https://github.com/reinderien/blockhood). 
Me doing dumb things with video games.

This time it's [Satisfactory](https://www.satisfactorygame.com). It's a very 
promising game in early-ish beta that is basically a modern version of
[Infinifactory](https://store.steampowered.com/app/300570/Infinifactory)
with, so far, somewhat less plot and significantly better graphics.

As with most other factory games, you make factories that crank out components
of ever-increasing complexity, requiring ever-increasing raw resources. The 
recipes governing these supply chains are documented in the wiki. The factory 
will grow in complexity and power needs as more of the tech tree is unlocked; in 
Satisfactory terminology this is divided into _tiers_.

One aspect of Satisfactory that is so far quite unique is the concept of 
clocking. Not only can factory buildings be speed-boosted ("overclocked") with 
special items, the building can be set to a _specific_ clock anywhere from 1% to
its maximum rate in increments of 1%. This is novel, interesting, allows for
much finer-grained efficiency tuning, and is a complete
[nerd snipe](https://xkcd.com/356).

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

Linear Analysis
---------------

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

I want to ask the solver something like:

- Consider a selected collection of recipes at the output - rotors, modular 
  frames and smart plating in this example
- Provide for each running at 100% clock, or having multiple instances whose
  clocks sum to 100%
- Tell me the minimum factory configuration needed to supply them

Translating this into GLPK speak,

- The _structural variables_ are in a vector, each element being a clock total
  of a given recipe; a.k.a. the sum of the clocks for all buildings running that 
  recipe
- The _auxiliary variables_ are in another vector, each element being the rate
  of production of a given resource (consumption is negative).
- The _bounds_ are the minimum and maximum for each of the structural and
  auxiliary variables. In the final answer, no resource rate can be negative, or 
  else the factory will run out. The bounds are also used to pre-set a desired 
  rate of production for a given resource or count of a given recipe.
- The _objective function_ is a scalar number, a cost to minimize, in our case
  the grand total of all clocks in constructors, assemblers, smelters and miners
- The _objective coefficients_ represent how expensive each recipe is to us.
  Currently this doesn't matter so we set every one to 1.
- The _constraint coefficient matrix_ is a grid of all resource rates for all
  recipes.

The vast majority of the code interacting with GLPK is to tell it about all of
the above. Once we have, getting to a solution is easy:

- Run the
  [Dantzig Simplex Algorithm](https://en.wikipedia.org/wiki/Simplex_algorithm)
  to get what GLPK calls a "basis" - an initial solution that allows for
  fractional clock counts
- To reduce the fractional clock counts to whole-integer percentages, run the
  [branch-and-bound](https://en.wikipedia.org/wiki/Branch_and_bound)
  MIP solver.
  
Linear Output
-------------

Currently - for a maximum tech tier of 2, and asking for the three output
recipes mentioned above - this produces:

```
Fetching recipe data...

GLPK Simplex Optimizer 5.0
23 rows, 28 columns, 51 non-zeros
      0: obj =   3.000000000e+02 inf =   2.283e+02 (3)
      6: obj =   1.917500000e+03 inf =   0.000e+00 (0)
OPTIMAL LP SOLUTION FOUND

GLPK Integer Optimizer 5.0
23 rows, 28 columns, 51 non-zeros
28 integer variables, none of which are binary
Integer optimization begins...
Long-step dual simplex will be used
+     6: mip =     not found yet >=              -inf        (1; 0)
Solution found by heuristic: 1918
+     6: mip =   1.918000000e+03 >=     tree is empty   0.0% (0; 1)
INTEGER OPTIMAL SOLUTION FOUND
Writing MIP solution to '/tmp/tmps17rs8ja'...
Problem:    satisfactory
Rows:       23
Columns:    28 (28 integer, 0 binary)
Non-zeros:  51
Status:     INTEGER OPTIMAL
Objective:  percentage_sum = 1918 (MINimum)

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
     9 Iron Ingot                  0             0               
    10 Iron Ore                    1             0               
    11 Iron Plate                  0             0               
    12 Iron Rod                    0             0               
    13 Leaves                      0             0               
    14 Limestone                   0             0               
    15 Modular Frame
                             3.33333             0               
    16 Mycelia                     0             0               
    17 Reinforced Iron Plate
                                   0             0               
    18 Rotor                 3.33333             0               
    19 Screw                       0             0               
    20 Smart Plating
                             3.33333             0               
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
    10 Iron Ingot   *            390             0               
    11 Iron Plate   *            150             0               
    12 Iron Rod     *            480             0               
    13 Modular Frame
                    *            100           100             = 
    14 Reinforced Iron Plate
                    *            100             0               
    15 Rotor        *            100           100             = 
    16 Screw        *            400             0               
    17 Smart Plating
                    *            100           100             = 
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
                    *              0             0               
    25 Iron Ore from Miner Mk. 1 on Pure node
                    *             98             0               
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

KKT.PB: max.abs.err = 5.68e-14 on row 19
        max.rel.err = 5.68e-14 on row 19
        High quality

End of output
```

Interpreting this:

- Any variable with an `=` to the right is a fixed recipe I'm asking for at the
  output
- _Activity_ for the row section is the resource rate per second times 100
- _Activity_ for the column section is the recipe's total clock

Non-linear Analysis
-------------------

[Coffeestain](https://www.coffeestainstudios.com) just had to throw a wrench in
the works. Input and output resources scale linearly based on the clock selected
for a building, but power does not. It scales with an exponent of 1.6. So we
have a secondary optimization problem classified as Mixed-Integer Nonlinear
Programming (MINLP). It's somewhat harder to find libraries for this. scipy just
can't, and GLPK just can't. One convenient solution is
[APMonitor](https://apmonitor.com), with its Python bindings in
[Gekko](https://gekko.readthedocs.io). APMonitor is apparently
pseudo-commercial, being distributed in binary form and requiring a paid license 
for some features; but the free level worked quite well for me and doesn't even 
come with nags.

Gekko has a sympy-like symbolic expression language that can directly translate
Python expressions to constraints and objectives, so it's definitely not as 
low-level as GLPK. Our problem setup becomes:

- We need to select the `APOPT` solver and tell it that we're using integer
  variables
- Make one integer variable for each non-zero recipe from the previous (linear)
  stage, which will represent the actual building count. The higher each of 
  these counts, the lower the individual (and total) power will be.
- Form an expression for each recipe, limiting the maximum clock for each
  building to 250% (the highest possible with three shards added).
- Form an expression for the total power based on the `**1.6` exponentiation.
- Form an expression for the total building count.
- Either minimize power, given a building limit; or minimize buildings, given a
  power limit. If we don't do this, the best solution for minimum power will be 
  to build infinite buildings for each recipe; and the best solution for minimum
  buildings will eat up a tonne of shards and power.

For this example the power limit has been set to 105 MW.
  
A successful run looks like:

```
 APMonitor, Version 0.9.2
 APMonitor Optimization Suite
 ----------------------------------------------------------------
 
 
 --------- APM Model Size ------------
 Each time step contains
   Objects      :            0
   Constants    :            0
   Variables    :           19
   Intermediates:            0
   Connections  :            0
   Equations    :           11
   Residuals    :           11
 
 Number of state variables:             19
 Number of total equations: -           10
 Number of slack variables: -           10
 ---------------------------------------
 Degrees of freedom       :             -1
 
 * Warning: DOF <= 0
 ----------------------------------------------
 Steady State Optimization with APOPT Solver
 ----------------------------------------------
Iter:     1 I:  0 Tm:      0.00 NLPi:   14 Dpth:    0 Lvs:    3 Obj:  2.18E+01 Gap:       NaN
Iter:     2 I:  0 Tm:      0.00 NLPi:    6 Dpth:    1 Lvs:    4 Obj:  2.25E+01 Gap:       NaN
...
Iter:    63 I:  0 Tm:      0.00 NLPi:    7 Dpth:    9 Lvs:   81 Obj:  2.31E+01 Gap:       NaN
Iter:    64 I:  0 Tm:      0.00 NLPi:    7 Dpth:    9 Lvs:   82 Obj:  2.31E+01 Gap:       NaN
--Integer Solution:   2.20E+01 Lowest Leaf:   2.20E+01 Gap:   1.92E-03
Iter:    65 I:  0 Tm:      0.00 NLPi:    2 Dpth:    9 Lvs:   82 Obj:  2.20E+01 Gap:  1.92E-03
 Successful solution
 
 ---------------------------------------------------
 Solver         :  APOPT (v1.0)
 Solution time  :   5.739999999059364E-002 sec
 Objective      :    22.0000000000000     
 Successful solution
 ---------------------------------------------------
```

Interpreting this, the optimizer runs very quickly until it finds the smallest
possible building count of 22. With a logging level of `INFO`, the program from
start to finish shows:

```
Loading recipe database up to tier 2...
28 recipes loaded.
Linear stage...
9 recipes in solution.
Nonlinear stage...
Minimizing buildings for at most 105 MW power:
Recipe                                   Clock  n P (MW)    tot shards tot s/out  tot s/extra
Iron Ingot                                  97  2   3.81   7.62      0   0   2.1  1.0       ∞
Iron Ingot                                  98  2   3.87   7.75      0   0   2.0  1.0       ∞
Iron Plate                                 150  1   7.65   7.65      1   1   2.0  2.0       ∞
Iron Rod                                   120  4   5.35  21.42      1   4   3.3  0.8       ∞
Modular Frame                               50  2   4.95   9.90      0   0  60.0 30.0    30.0
Reinforced Iron Plate                       50  2   4.95   9.90      0   0  24.0 12.0       ∞
Rotor                                       50  2   4.95   9.90      0   0  30.0 15.0    30.0
Screw                                      100  4   4.00  16.00      0   0   1.5  0.4       ∞
Smart Plating                               50  2   4.95   9.90      0   0  60.0 30.0    30.0
Iron Ore from Miner Mk. 1 on Pure node      98  1   4.84   4.84      0   0   0.5  0.5   100.0
Total                                          22        104.86          5 
```
