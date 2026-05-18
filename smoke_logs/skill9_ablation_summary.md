| cfg | sdk | n | avg_q | cap | cool | P | R | F1 | custom | guidance | both |
|-----|-----|---:|---:|---:|---:|---:|---:|---:|:---:|:---:|:---:|
| split | claude | 6 | 3.83 | 1 | 0 | 0.70 | 0.67 | 0.68 | Y | Y | Y |
| split | codex | 6 | 4.83 | 0 | 0 | 0.76 | 0.92 | 0.83 | Y | Y | Y |
| split_HEKJ | claude | 6 | 7.67 | 0 | 0 | 0.00 | 0.00 | 0.00 |  |  |  |
| split_HEKJ | codex | 6 | 4.00 | 0 | 0 | 0.00 | 0.00 | 0.00 |  |  |  |
| split_JK | claude | 6 | 7.83 | 0 | 0 | 0.00 | 0.00 | 0.00 |  |  |  |
| split_JK | codex | 6 | 4.17 | 0 | 0 | 0.00 | 0.00 | 0.00 |  |  |  |
| split_JKF | claude | 6 | 0.00 | 0 | 0 | 0.00 | 0.00 | 0.00 |  |  |  |
| split_JKF | codex | 6 | 1.67 | 0 | 0 | 0.00 | 0.00 | 0.00 |  |  |  |
| split_M | claude | 6 | 3.00 | 0 | 0 | 0.00 | 0.00 | 0.00 |  |  |  |
| split_M | codex | 6 | 4.33 | 0 | 0 | 0.00 | 0.00 | 0.00 |  |  |  |

```
recommended_cfg=split  pareto_score=2.095
claude: P=0.70 R=0.67 custom=True guidance=True both=True
codex: P=0.76 R=0.92 custom=True guidance=True both=True
```
