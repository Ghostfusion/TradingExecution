# The Economics and Reality of Retail/Prop Intraday Trading — Evidence Brief

**Bottom line.** The base rate is brutal and stable across two decades and three continents: 70-97% of persistent retail day traders lose money net of costs, and only ~1-3% are durably profitable [1][2][3][4][23]. The edge that survives is small (top-decile Taiwan traders: +37.9 bps/day net [2]), fragile to costs (double-digit bps round trips on anything below mega-cap [11][21]), and taxed as ordinary income unless Section 475(f) is elected [25][26]. Every dollar of expected return must be underwritten against a 2-10 bp round-trip cost floor and a Sharpe ceiling near 1.0 after costs [20][30]. The reason to run an intraday sleeve anyway is **non-overlapping risk**: intraday equity return streams are empirically low- or negative-beta to the overnight/value factors your swing sleeve owns [16][17][18].

Structure: Findings (1-30) -> Numbers to design against -> Contested or unproven -> Implications for our system -> Sources. Evidence from futures/non-US markets is flagged inline.

---

## Findings

1. **Brazilian mini-index futures day traders: 97% of persisters lost money.** Chague, De-Losso & Giovannetti tracked **19,646** individuals who began day trading mini-Ibovespa futures 2013-2015; of the **1,551** who persisted >300 trading days, **97% lost money**, **1.1%** earned more than the Brazilian minimum wage, **0.5%** more than a bank teller's starting salary; only **3.0%** ended with positive net profit. Authors conclude day trading for a living is "virtually impossible." *PROVEN, but instrument = futures, not US cash equities.* [1]

2. **Barber, Lee, Liu & Odean (Taiwan 1992-2006): <1% predictably profitable net of fees.** Only a small minority earn reliable positive abnormal returns net of costs; the **top 500** prior-year performers averaged **+61.3 bps/day before and +37.9 bps/day after fees**, while bottom-ranked traders lost money the next year; ~**4,000 traders (~1%)** show repeatable profitability; ~**5%** are profitable in any given period. *PROVEN, Taiwan, low retail cost structure.* [2][3]

3. **US discount-broker evidence (Barber & Odean 2000): trading intensity destroys returns.** Most-active quintile **11.4%/yr** vs least-active **18.5%/yr**, market **17.9%** — underperformance **~6.5 pp/yr** (risk-adjusted 1.8-6.8%/yr). *PROVEN, 1991-1996, pre-zero-commission.* [4]

4. **Learning is weak and exit slow.** Taiwan: **44%** survive 1yr, **24%** 2yr, **15%** 3yr; **>75% quit within 2 years**; **95.3%** of previously unprofitable and **96.4%** of profitable traders with 50+ days experience trade again within 12 months. *PROVEN.* [3]

5. **Retail-trades-predict-but-lose paradox.** Barber, Lin & Odean: retail order imbalance predicts short-horizon returns, yet a long-short quintile strategy earns **-15.3%/yr** in heavy-retail stocks and **+6.8%/yr** elsewhere (journal: **-14.8%/+6.6%**). *PROVEN.* [5]

6. **2024 regulator datapoints (non-US, options/futures):** SEBI: **91%** of Indian retail F&O traders lost in FY2024-25, **93%** over FY2022-24; CFTC 2024: median US retail *futures* trader lost money, only ~60th percentile breaks even. *REPORTED; India options + US futures, NOT US cash equities.* [23][24]

7. **Cost migrated, not disappeared.** Zero commissions -> spread/PFOF/price improvement. Wholesalers price-improve **76% of marketable orders** at ~**47% of quoted spread** (~**51%** in S&P 500 vs ~**3%/1.4 bps** on exchanges); 2024 SEC discussion cites ~**0.5 bps** average retail benefit. *REPORTED.* [21]

8. **Spreads (US cash equities 2024-25).** Mega-caps ~**1 bp** quoted; S&P 500 ~**4.5-7.3 bps** quoted; institutional **effective ~3.9-4.1 bps**; retail limit large-cap as low as **0.7 bps**. Effective < quoted due to price improvement. *REPORTED.* [11]

9. **Regulatory fees second-order.** SEC §31 **$27.80/$1M** through 2025-05-13 then **$0.00** from 2025-05-14; FINRA TAF **$0.000166/share** cap **$8.30**. On $50k sale of $50 stock: TAF ~$0.17 (0.03 bps), §31 ~$1.39 (0.28 bps). *PROVEN.* [11][12]

10. **Leverage rules changed 2026.** **$25,000 PDT minimum, 4-trades-in-5-days test and PDT designation eliminated** via FINRA Rule 4210 amendments effective **2026-06-04** (phase-in to **2027-10-20**), replaced by intraday margin. Reg T 50% initial (2:1); maintenance floor 25% (4:1 theoretical); house higher. *PROVEN; broker rollout may lag.* [10][29]

11. **Leverage is costly.** IBKR Pro USD margin ~**5.13% APR** at $25k tier in 2025 (benchmark+1.5%), **4.13-4.80%** higher tiers; Lite **6.13%**. *REPORTED.* [13]

12. **Short borrow scales with difficulty.** GC **0.05-0.50%/yr**; HTB **5-50%+**, extremes >100%; accrues daily; dividends-in-lieu, locates, margin interest stack. *REPORTED.* [14]

13. **Published alpha decays.** McLean & Pontiff: **26% lower out-of-sample, 58% lower post-publication**; publication effect ~**32 pp**; 2013 WP ~**35%**. *PROVEN (magnitude contested).* [6]

14. **Factor decay is valuation-driven.** Arnott, Beck & Kalesnik: past factor returns predict poorly, starting valuations informative; high valuations imply low forward returns; mechanism is richness/valuation, not a clean crowding test. *REPORTED/CONTESTED.* [7]

15. **Crowding synchronizes drawdowns.** MSCI on the **2025 quant wobble**: unusual factor-correlation regimes (beta, profitability, momentum, liquidity) + partial crowding unwind; Goldman put quant-equity losses ~**4.2%**; momentum L/S **>3%**. *REPORTED.* [8]

16. **2026 momentum/AI unwind.** Reuters: systematic L/S gave back ~a quarter of YTD gains in mid-2026 reversal — **+14.4% (Jun 22) to +10.8% YTD**, worst damage in the **short book**; systematic macro/CTA still positive. *REPORTED (single-desk estimates).* [9]

17. **Cheap-to-arbitrage intraday edges.** Decay logic that halves post-publication factor returns applies *more* strongly to intraday signals (lower capacity, faster dissemination); assume any documented intraday anomaly is already decayed unless the mechanism is capacity/risk-limited. *INFERENCE from [6][7][8].*

18. **Intraday vs overnight decomposition.** Lou, Polk & Skouras: predictors map differently into **open-to-close** vs **close-to-open**; valuation-driven equity-premium mean reversion is **primarily intraday**; aggregate premium accrues disproportionately **overnight**. *PROVEN (US).* [16]

19. **Intraday momentum without reversal; overnight reversal without momentum.** Barardehi, Bogousslavsky & Muravyev. *PROVEN (US).* [18]

20. **Intraday reversal is cost-fragile.** Heston-Korajczyk-Sadka tie it to liquidity imbalances/bid-ask bounce; cost erosion concentrated in small caps; large-cap low-turnover implementations report **~30-50 bps/week net**; a microcap reversal breaks even at **5.39%** cost. *CONTESTED.* [19]

21. **Market intraday momentum small.** Gao, Han, Li & Zhou: first half-hour predicts last half-hour (SPY 1993-2013), **R² ~1.6%**, stronger on high-vol/high-volume/macro-release days. *PROVEN, single ETF.* [20]

22. **Day-trading returns are low-beta.** Aggregate day-trading **beta ~0.26**; positive beta-return relation overnight, negative during trading hours. *REPORTED.* [15]

23. **Backtest overfitting is measurable.** Bailey & Lopez de Prado DSR corrects for multiple testing/non-normality; **PBO>0.5** = in-sample winner likely below-median OOS; **DSR>0.95** bar. *PROVEN method.* [22]

24. **Realistic retail net performance.** **8-15% net CAGR** is a strong result; **Sharpe ~1.0 after costs** solid; **>2** exceptional/rare; live ~**50-70% of backtest** Sharpe. *REPORTED (practitioner consensus).* [30]

25. **Base rate of failure.** Hedge funds **~7.4% yr1, ~20.3% yr2**, ~**30% by 3yr**, annual liquidation **5-9%**. Prop evaluations **~14% pass, ~7% ever payout** (FPFX, 300k+ accounts). Day traders **>75% quit within 2 years**. *REPORTED.* [3][27]

26. **Costs and taxes quiet killers.** Short-term gains at **ordinary rates (10-37%)**; **3.8% NIIT** above $200k/$250k AGI; **wash-sale** 61-day window (30 before + sale + 30 after) penalizes re-entry; **§475(f)** removes wash-sale rules and $3,000 cap, converts to ordinary (Form 4797), filed by prior-year due date. *PROVEN.* [25][26]

27. **Copy/social trading worse.** 2025 study: platform average **-7.67 bps**; non-mirror **+1.15** vs mirror **-10.98**; mirrored trades **-61.24** vs same users' own trades **-6.88**. Older eToro copy-portfolio result (21/28 positive alpha 2017-2020) is stale/contested. *PROVEN (2025 study).* [28]

28. **Order path for a Python daemon.** Alpaca-class: **~20 ms** internal ingestion, order submit **~120-250 ms**, **~200-800 ms** quote-then-submit serially; WebSocket `trade_updates` fastest fill path; `feed=sip` needed for full NBBO. Seconds-to-subsecond, **not HFT**. *REPORTED.* [31]

29. **Broker/platform downtime recurring.** Alpaca 2025-2026: recurring **5xx on order creation** (2026-09-11), funding/transfer errors (2026-09-03), Broker API latency/timeouts (2026-08-29), upstream-venue order rejections (2026-08-27), sign-up errors (2026-08-19), ~1h incident 2025-09-23. Robinhood partial outage 2025-10-06 (~45 min-2h); Schwab/Fidelity 2024-08-05 (Schwab ~15,000 reports; IBKR none). *PROVEN that incidents occur.* [31][32]

30. **3-6 month program cannot prove a low-Sharpe edge.** Sharpe uncertainty ~**1/sqrt(n)**: 95% bars ~**100 trades (Sharpe 1.0), ~400 (0.5), >1,600 (0.25)**; paper overstates live **10-30%** (day systems cited **8-15% annual inflation**). Validates plumbing/risk, not statistical significance. *PROVEN statistics; REPORTED magnitudes.* [22][33]

---

## Numbers to design against

### A. All-in round-trip cost, US cash equities (marketable orders, retail size)

| Liquidity bucket | ADV proxy | Quoted spread | Effective (one-way) | **All-in round trip** | Notes |
|---|---|---|---|---|---|
| Mega-cap | >$20B ADV | ~1 bp | 0.7-1.5 bps | **2-5 bps** | Wholesaler price improvement beats quote [11][21] |
| Large-cap | $500M-$20B ADV | 4.5-7.3 bps | 3.9-4.1 bps | **5-12 bps** | Institutional effective-spread benchmark [11] |
| Mid-cap | $50M-$500M ADV | 8-20 bps | ~6-15 bps | **12-25 bps** | §31+TAF add <0.5 bp at retail size [11][12] |
| Small-cap | <$50M ADV | 20-60+ bps | 15-50 bps | **25-60+ bps** | Cost can exceed a 25 bps/day edge [19] |
| Shorts (any) | - | + spread | + borrow | **add 0-50%+/yr** | HTB 5-50%+, GC 0.05-0.50% [14] |
| Levered portion | - | - | - | **add 4.1-6.1% APR** | IBKR Pro ~5.13%, Lite 6.13% [13] |

Design rule: require gross signal >= **4x** assumed round-trip cost (>= 8-48 bps depending on bucket). *INFERENCE.*

### B. Expected % profitable (by sample)

| Population | N | % profitable | Source | Caveat |
|---|---|---|---|---|
| Brazil mini-index futures, >300d | 1,551 of 19,646 | **3.0% net; 1.1% > min wage; 0.5% > teller** (97% lost) | Chague et al. [1] | Futures, Brazil |
| Taiwan day traders 1992-2006 | full market | **<1% predictably net-profitable; ~5% in a period** | Barber et al. [2][3] | Taiwan equities |
| US discount-broker households | 66,465 | most-active quintile **11.4%/yr** vs 18.5% | Barber & Odean [4] | pre-zero-comm |
| India retail F&O FY2024-25 | regulator-wide | **91% lost** | SEBI [23] | leveraged options |
| CFTC retail futures 2024 | - | median lost; ~60th pctile breaks even | CFTC [24] | futures |
| Prop evaluations | 300k+ accounts | **~14% pass; ~7% ever payout** | FPFX [27] | prop, not personal capital |

### C. Sizing/leverage ceiling

| Constraint | Value | Source |
|---|---|---|
| Reg T initial margin | **50% (2:1)** | [10] |
| Maintenance margin floor | **25% (4:1 theoretical)** | [10] |
| Old PDT day-trading buying power | 4x (eliminated 2026-06-04) | [10][29] |
| New intraday margin framework | effective 2026-06-04, phase-in to 2027-10-20 | [29] |
| Evidence-implied ceiling | **gross <=1.0-1.5x, per-name <=2% NAV, <=20% NAV/sector** | INFERENCE from [1][27] |
| Margin call mechanics | **5 business days**; then 2x maintenance; then **90-day cash-only** | [10] |

### D. Base rate of failure

| Event | Base rate | Source |
|---|---|---|
| Hedge fund failure, yr 1 | **~7.4%** | [27] |
| Hedge fund failure, yr 2 | **~20.3%** | [27] |
| Hedge fund failure by 3 yr | **~30%** | [27] |
| Prop pass / payout | **~14% / ~7%** | [27] |
| Day-trader survival | **44% (1yr), 24% (2yr), 15% (3yr)** | [3] |
| Design base rate: new intraday strategy dead/retired within 12 months | **assume 50-75%** | INFERENCE from [3][22][27] |

### E. Honest net CAGR / Sharpe for a competent single operator

| Tier | Net CAGR | Net Sharpe (after costs) | Frequency |
|---|---|---|---|
| Failing / dead | <0% | <0 | Most likely in yr 1 [1][27] |
| Mediocre but alive | 0-5% | 0.2-0.5 | Common |
| **Solid** | **5-15%** | **~1.0** | Good, rare [30] |
| Exceptional | 15-25% | 1.0-1.5 | Very rare, fragile [30] |
| Suspicious | >25% | >2.0 | Assume overfit [22][30] |
| Live vs backtest | - | **live ~50-70% of backtest Sharpe** | [30] |

### F. Operational thresholds

| Item | Value | Source |
|---|---|---|
| Order submit latency | **~120-250 ms**; **~200-800 ms** quote-then-submit | [31] |
| Internal data ingestion | **~20 ms** | [31] |
| Market-data feed | free = IEX-only; use `feed=sip` | [31] |
| Fill notification | WebSocket `trade_updates` > REST polling | [31] |
| Alpaca incident cadence | multiple 2025-2026; sub-minute to ~1h; 5xx on order creation recurring | [31][32] |
| Peer platform outages | Robinhood 2025-10-06 (~45min-2h); Schwab/Fidelity 2024-08-05 | [32] |

---

## Contested or unproven

- **Magnitude of post-publication decay:** McLean-Pontiff 58% vs 2013 WP 35%; later papers ~one-half. Direction robust, point estimate not. Arnott/Beck/Kalesnik crowding story is consistent with, not a clean test of, decay [6][7].
- **"Day trading is zero-sum/negative-sum after costs"** widely repeated, often unsourced; cleanest support is the accounting identity plus measured cost drag [1][5].
- **2026 momentum unwind numbers** (+14.4%->+10.8% YTD, ~4.2% quant drawdown) are single-desk Goldman estimates relayed by news, not audited index data [8][9].
- **Diversification value of intraday sleeves** rests on a handful of beta/correlation results (aggregate beta ~0.26 [15]); the exact intraday-vs-value correlation for *our* universe is **unmeasured** and must be estimated empirically [15][16][19].
- **Paper-vs-live gap "10-30%" / "8-15% annual inflation"** is practitioner consensus without a controlled study; defensible claim is that paper fills are optimistic and the gap is positive [33].
- **Cost figures** are venue/order-type/sample dependent: large-cap effective spread reported from **0.7 bps** (retail limit) to **4.1 bps** (institutional) [11][21].
- **Non-US/non-equity evidence** (Brazil futures, India options, CFTC futures, prop evaluations) is structurally harsher than US cash equities and must not be quoted as a US-equity base rate [1][23][24][27].
- **Prop-firm pass rates** are almost entirely third-party estimates; FTMO publishes no official figure [27].

---

## Implications for our system

1. **Treat the intraday sleeve as an unproven experiment with a hard cost budget.** Cap its capital at the smaller sleeve's allocation (e.g. 30%) and require it to clear **>=4x modeled round-trip cost per trade** before sizing up — encode as a pre-trade *cost-clearance* gate alongside the existing mandate gates.
2. **Build a first-class cost model module** (table A as config: quoted/effective spread, §31 with the 2025-05-14 zero-rate toggle, TAF $0.000166/sh cap $8.30, per-symbol borrow, margin APR) shared by backtest and live loop. Any backtest not run through it is inadmissible evidence.
3. **Keep the two sleeves capital-partitioned and risk-gated separately, then summed.** House risk gate (CVaR, drawdown, correlation stress, vol regime, knife guard) sits above both; the intraday sleeve must never consume the swing sleeve's reserved capital — 70/30 as *hard partitions* the gate can reduce but never transfer intraday.
4. **Instrument the Opportunity-Score vs Trade-Permission distinction at the sleeve level, not just ticker level.** The intraday sleeve needs its own "weak signal" vs "house risk forbids" split with the binding gate named in the same JSON contract as the mandates.
5. **Leverage ceiling gross <=1.5x, per-name <=2% NAV, no single-name intraday notional above 5% gross.** The 2:1 Reg T and 4:1 maintenance floors are regulatory, not evidence-based; leverage accelerates the failure base rate (findings 12, 25, 29). Track intraday margin excess continuously and pre-empt the 5-day margin-call clock.
6. **Elect Section 475(f) before the applicable due date if the sleeve trades >~a few hundred round trips/year.** Without it, wash-sale loss disallowance (61-day window) and the $3,000 capital-loss cap punish exactly the re-entry behavior a day sleeve generates. Model after-tax P&L at >=24-37% ordinary + 3.8% NIIT.
7. **Add a crowding/decay monitor to the alpha layer.** Track rolling live-vs-modeled slippage and edge-per-trade; auto-throttle/retire a signal when realized edge falls below 2x modeled cost for N consecutive sessions. Treat single-session edge spikes as potential publication-decay events.
8. **Design for broker-API failure as the normal case.** Daemon must reconcile open orders/positions at start-up, treat a stale `trade_updates` stream as a kill-switch trigger, fail-closed on any order-creation 5xx, never assume ack = fill. Monitor: connection health, heartbeat age, fill-vs-ack divergence, open-order drift vs broker state.
9. **Run the 3-6 month program as plumbing/risk certification, not a significance test.** Pre-register promotion criteria: >=250 live trades, live slippage within 1.5x model, zero unexplained order/position mismatches, max drawdown within house limit, DSR>0.95 on combined paper+live sample. Below ~100 trades you cannot distinguish Sharpe 1.0 from noise.
10. **Adopt the house-level KPI that makes the two-sleeve structure self-justifying.** Dashboard must show rolling 60-day correlation between sleeve return streams and the value/momentum factors the swing sleeve owns; if correlation drifts toward +1 the diversification rationale is gone and the sleeve should be shrunk — a monthly gate, not a report.

---

## Sources

1. Chague, F., De-Losso, R., & Giovannetti, B. — "Day Trading for a Living?" — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101 (2019, rev. 2020).
2. Barber, B. M., Lee, Y.-T., Liu, Y.-J., & Odean, T. — "The Cross-Section of Speculator Skill: Evidence from Day Trading" — Journal of Financial Markets — https://faculty.haas.berkeley.edu/odean/papers/Day%20Traders/Day%20Trading%20and%20Learning%20110214.pdf (2014).
3. Barber, B. M., Lee, Y.-T., Liu, Y.-J., Odean, T., & Zhang, K. — "Do Day Traders Rationally Learn About Their Ability?" — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2856963 (2019).
4. Barber, B. M., & Odean, T. — "Trading Is Hazardous to Your Wealth" — Journal of Finance — https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00226 (2000).
5. Barber, B. M., Lin, S., & Odean, T. — "Resolving a Paradox: Retail Trades Positively Predict Returns but Are Not Profitable" — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4034315 (2023; JFQA 2024).
6. McLean, R. D., & Pontiff, J. — "Does Academic Research Destroy Stock Return Predictability?" — Journal of Finance — https://onlinelibrary.wiley.com/doi/10.1111/jofi.12365 (2016).
7. Arnott, R., Beck, N., & Kalesnik, V. — "Forecasting Factor and Smart Beta Returns (Hint: History Is Worse than Useless)" — Research Affiliates — https://www.researchaffiliates.com/publications/articles/222-forecasting-factor-and-smart-beta-returns (2016).
8. MSCI — "Understanding the 2025 Quant Wobble" — https://www.msci.com/research-and-insights (2025).
9. Reuters — "Quant funds hit by momentum/AI unwind" — https://www.reuters.com/markets/ (2026).
10. FINRA / SEC — Regulation T and Rule 4210 margin framework — https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210 (2026).
11. SEC — Rule 605/606 execution-quality data; Division of Trading & Markets effective-spread analyses — https://www.sec.gov/marketstructure (2024-2025).
12. SEC — Section 31 fee-rate schedule; FINRA Trading Activity Fee — https://www.sec.gov/ofm/Article/fee_rate_adjustment.html and https://www.finra.org/rules-guidance/guidance/trading-activity-fee (2025).
13. Interactive Brokers — Margin Interest Rates — https://www.interactivebrokers.com/en/pricing/margin-interest-rates.php (2025).
14. Industry/academic short-borrow-cost summaries (stock-loan GC vs HTB) — https://www.sec.gov/investment (2024-2025).
15. "Asset pricing: A tale of night and day" and aggregate day-trading beta estimates — https://www.sciencedirect.com (2020).
16. Lou, D., Polk, C., & Skouras, S. — "A Tug of War: Overnight versus Intraday Expected Returns" — Journal of Financial Economics — https://www.sciencedirect.com/science/article/pii/S0304405X19300756 (2019).
17. Bogousslavsky, V. — "Infrequent Rebalancing, Return Autocorrelation, and Seasonality" — Journal of Finance — https://onlinelibrary.wiley.com/doi/10.1111/jofi.12436 (2016).
18. Barardehi, Y., Bogousslavsky, V., & Muravyev, D. — "What Drives Momentum and Reversal? Evidence from Day and Night Signals" — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3757847 (2021).
19. Heston, S., Korajczyk, R., & Sadka, R. — intraday reversal / trading-cost sensitivity literature — https://www.sciencedirect.com (2010 onward).
20. Gao, L., Han, Y., Li, S. Z., & Zhou, G. — "Market Intraday Momentum" — Journal of Empirical Finance — https://www.sciencedirect.com/science/article/abs/pii/S0927539818300530 (2018).
21. US wholesaler price-improvement evidence (Citadel Securities, Virtu disclosures; SEC DERA discussion paper) — https://www.sec.gov (2024).
22. Bailey, D. H., & Lopez de Prado, M. — "The Deflated Sharpe Ratio" — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551 (2014).
23. SEBI — Study on individual traders in the equity F&O segment (FY2022-FY2025) — https://www.sebi.gov.in (2024-2025).
24. CFTC — Staff paper on retail futures trader performance — https://www.cftc.gov (2024).
25. IRS — Publication 550 (wash sales) — https://www.irs.gov/publications/p550 (2025).
26. IRS — Topic No. 429, Traders in Securities (Section 475(f)) — https://www.irs.gov/taxtopics/tc429 (2025).
27. Hedge-fund survival/liquidation and prop-firm evaluation base rates (academic + FPFX Technology dataset summaries) — https://www.fpfxtechnology.com (2024-2025).
28. 2025 social/mirror-trading performance study (eToro-style copy trading) — https://papers.ssrn.com (2025).
29. FINRA — Regulatory Notice on elimination of the Pattern Day Trader framework / intraday margin (Rule 4210 amendments; effective 2026-06-04) — https://www.finra.org/rules-guidance/notices (2025-2026).
30. Practitioner consensus on realistic retail algo net returns and Sharpe — https://www.quantstart.com (2024-2025).
31. Alpaca — Status page and incident history; API/docs and community latency reports — https://status.alpaca.markets (2025-2026).
32. Reuters — Brokerage outage coverage: Schwab/Fidelity (2024-08-05); Robinhood (2025-10-06) — https://www.reuters.com/markets (2024-2025).
33. Paper-trading vs live-trading performance gap (practitioner analyses) — https://www.quantstart.com (2024-2025).

---

*Prepared 2026-09-12. Every percentage labeled "US cash equities" is sourced to a US-equity sample; futures/options/Taiwan/Brazil/India/prop samples are flagged inline. Combine with the validation brief for the statistical-power arithmetic of the 3-6 month promotion program.*
