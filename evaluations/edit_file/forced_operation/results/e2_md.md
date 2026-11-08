# Experiment 2 (e2, e2b): 54 attempts

| exp | arm | final | first-try | payload o200k | completion | latency s | truncated |
|---|---|---|---|---|---|---|---|
| E2a | Wfull | 18.5% [11.1, 33.3] | 18.5% [11.1, 33.3] | 1994 [175, 4977] | 9938 [5384, 14070] | 146.3 [82.3, 200.0] | 18.5% [11.1, 22.2] |
| E2a | Wappend | 14.8% [11.1, 22.2] | 11.1% [11.1, 11.1] | 107 [40, 225] | 12570 [11333, 14233] | 195.9 [165.4, 230.2] | 33.3% [33.3, 33.3] |
| E2b | Wfull | n/a | n/a | n/a | n/a | n/a | n/a |
| E2b | Wref | n/a | n/a | n/a | n/a | n/a | n/a |
| E2c | Wfull | n/a | n/a | n/a | n/a | n/a | n/a |
| E2c | Eedit | n/a | n/a | n/a | n/a | n/a | n/a |

| paired | d final | d first | d payload | d completion | d latency s |
|---|---|---|---|---|---|
| E2a|Wappend-Wfull | -3.7% [-11.1, 0.0] | -7.4% [-22.2, 0.0] | -1887 [-4937, -118] | 2632 [-2738, 8849] | 49.6 [-34.6, 148.0] |
| E2a|L|Wappend-Wfull | 0.0% [0.0, 0.0] | n/a | -4937 [-4937, -4937] | -2738 [-2738, -2738] | -34.6 [-34.6, -34.6] |
| E2a|M|Wappend-Wfull | -11.1% [-11.1, -11.1] | n/a | -606 [-606, -606] | 8849 [8849, 8849] | 148.0 [148.0, 148.0] |
| E2a|S|Wappend-Wfull | 0.0% [0.0, 0.0] | n/a | -118 [-118, -118] | 1785 [1785, 1785] | 35.3 [35.3, 35.3] |
| E2b|Wref-Wfull | n/a | n/a | n/a | n/a | n/a |
| E2c|Eedit-Wfull | n/a | n/a | n/a | n/a | n/a |

| exp | arm | size | final | payload | completion | latency |
|---|---|---|---|---|---|---|
| E2a | Wfull | L | 11.1% [11.1, 11.1] | 4977 [4977, 4977] | 14070 [14070, 14070] | 200.0 [200.0, 200.0] |
| E2a | Wfull | M | 33.3% [33.3, 33.3] | 831 [831, 831] | 5384 [5384, 5384] | 82.3 [82.3, 82.3] |
| E2a | Wfull | S | 11.1% [11.1, 11.1] | 175 [175, 175] | 10361 [10361, 10361] | 156.8 [156.8, 156.8] |
| E2a | Wappend | L | 11.1% [11.1, 11.1] | 40 [40, 40] | 11333 [11333, 11333] | 165.4 [165.4, 165.4] |
| E2a | Wappend | M | 22.2% [22.2, 22.2] | 225 [225, 225] | 14233 [14233, 14233] | 230.2 [230.2, 230.2] |
| E2a | Wappend | S | 11.1% [11.1, 11.1] | 57 [57, 57] | 12145 [12145, 12145] | 192.0 [192.0, 192.0] |

Per model final success (successes/attempts): E2a|Wappend|claude-opus-5 1/9; E2a|Wappend|glm-5.3 0/9; E2a|Wappend|kimi-k3 3/9; E2a|Wfull|claude-opus-5 1/9; E2a|Wfull|glm-5.3 4/9; E2a|Wfull|kimi-k3 0/9
