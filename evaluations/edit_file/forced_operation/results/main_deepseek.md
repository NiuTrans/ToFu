# Results (main): 34 attempts, models deepseek-v4-pro
Missing (HTTP errors after retries): 8

## Pooled over models (equal weight), 95% task-cluster bootstrap CI

| arm | final success | first-try success | payload o200k / attempt | provider completion tokens | schema-refused first call | model calls |
|---|---|---|---|---|---|---|
| A1 | 100.0% [100.0, 100.0] | 93.3% [78.6, 100.0] | 102.5 [69.5, 151.5] | 367 [265, 488] | 0.0% [0.0, 0.0] | 1.07 [1.00, 1.21] |
| A2 | 100.0% [100.0, 100.0] | 100.0% [100.0, 100.0] | 74.0 [57.9, 95.2] | 474 [190, 1034] | 0.0% [0.0, 0.0] | 1.00 [1.00, 1.00] |
| A3 | 75.0% [25.0, 100.0] | 75.0% [25.0, 100.0] | 89.5 [65.5, 114.2] | 366 [182, 714] | 0.0% [0.0, 0.0] | 1.25 [1.00, 1.75] |
| A4 | 100.0% [100.0, 100.0] | 100.0% [100.0, 100.0] | 66.0 [47.0, 86.3] | 188 [141, 250] | 0.0% [0.0, 0.0] | 1.00 [1.00, 1.00] |

## Per model

| arm | model | final | first-try | payload | completion |
|---|---|---|---|---|---|
| A1 | deepseek-v4-pro | 100.0% [100.0, 100.0] | 93.3% [78.6, 100.0] | 102.5 [69.1, 151.2] | 367 [264, 489] |
| A2 | deepseek-v4-pro | 100.0% [100.0, 100.0] | 100.0% [100.0, 100.0] | 74.0 [58.3, 95.0] | 474 [187, 1033] |
| A3 | deepseek-v4-pro | 75.0% [25.0, 100.0] | 75.0% [25.0, 100.0] | 89.5 [65.5, 114.2] | 366 [182, 714] |
| A4 | deepseek-v4-pro | 100.0% [100.0, 100.0] | 100.0% [100.0, 100.0] | 66.0 [47.3, 86.9] | 188 [141, 250] |

## Paired differences (row arm minus column arm), pooled

| pair | subset | d final | d first-try | d payload | d completion |
|---|---|---|---|---|---|
| A2-A1 | insert | n/a | n/a | n/a | n/a |
| A2-A1 | insert|S | n/a | n/a | n/a | n/a |
| A2-A1 | insert|M | n/a | n/a | n/a | n/a |
| A2-A1 | insert|L | n/a | n/a | n/a | n/a |
| A2-A1 | replace | n/a | n/a | n/a | n/a |
| A2-A1 | replace|S | n/a | n/a | n/a | n/a |
| A2-A1 | replace|M | n/a | n/a | n/a | n/a |
| A2-A1 | replace|L | n/a | n/a | n/a | n/a |
| A2-A1 | delete | n/a | n/a | n/a | n/a |
| A2-A1 | delete|S | n/a | n/a | n/a | n/a |
| A2-A1 | delete|M | n/a | n/a | n/a | n/a |
| A2-A1 | delete|L | n/a | n/a | n/a | n/a |
| A2-A1 | multi | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | -35.0 [-35.0, -35.0] | 1381 [1381, 1381] |
| A2-A1 | multi|S | n/a | n/a | n/a | n/a |
| A2-A1 | multi|M | n/a | n/a | n/a | n/a |
| A2-A1 | multi|L | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | -35.0 [-35.0, -35.0] | 1381 [1381, 1381] |
| A2-A1 | all | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | -35.0 [-35.0, -35.0] | 1381 [1381, 1381] |
| A3-A1 | insert | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | -8.0 [-8.0, -8.0] | 581 [581, 581] |
| A3-A1 | replace | n/a | n/a | n/a | n/a |
| A3-A1 | delete | n/a | n/a | n/a | n/a |
| A3-A1 | multi | n/a | n/a | n/a | n/a |
| A3-A1 | all | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] | -8.0 [-8.0, -8.0] | 581 [581, 581] |
| A4-A1 | insert | n/a | n/a | n/a | n/a |
| A4-A1 | replace | n/a | n/a | n/a | n/a |
| A4-A1 | delete | n/a | n/a | n/a | n/a |
| A4-A1 | multi | n/a | n/a | n/a | n/a |
| A4-A1 | all | n/a | n/a | n/a | n/a |
| A4-A3 | insert | n/a | n/a | n/a | n/a |
| A4-A3 | replace | 100.0% [100.0, 100.0] | 100.0% [100.0, 100.0] | 2.0 [2.0, 2.0] | 163 [163, 163] |
| A4-A3 | delete | n/a | n/a | n/a | n/a |
| A4-A3 | multi | n/a | n/a | n/a | n/a |
| A4-A3 | all | 100.0% [100.0, 100.0] | 100.0% [100.0, 100.0] | 2.0 [2.0, 2.0] | 163 [163, 163] |
| A3-A2 | insert | n/a | n/a | n/a | n/a |
| A3-A2 | replace | n/a | n/a | n/a | n/a |
| A3-A2 | delete | n/a | n/a | n/a | n/a |
| A3-A2 | multi | n/a | n/a | n/a | n/a |
| A3-A2 | all | n/a | n/a | n/a | n/a |

## Failure taxonomy (all models)

- A1 (n=15): first tool error {'ambiguous': 1}; wrong final {}; tool said OK but file wrong 0
- A2 (n=8): first tool error {}; wrong final {}; tool said OK but file wrong 0
- A3 (n=4): first tool error {}; wrong final {'other_wrong': 1}; tool said OK but file wrong 1
- A4 (n=7): first tool error {}; wrong final {}; tool said OK but file wrong 0
