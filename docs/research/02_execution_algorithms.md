# Execution & Transaction-Cost Modeling for Intraday US Equities

**Bottom line (4 lines)**
1. Almgren–Chriss is a *risk/cost optimiser*, not a cost model; its linear-impact assumption is explicitly rejected by its own successor paper — the temporary-impact exponent is 3/5 (β = 0.600 ± 0.038), and the square-root β = 1/2 is rejected at 95% confidence [2].
2. For whole metaorders the robust law is `impact_bps ≈ 10,000 · Y · σ_daily · √(Q/V)` with Y ≈ 0.5–1.0; that is the number to put in a pre-trade gate, and it is roughly schedule-independent outside high participation [3][4][5][6].
3. At our size (USD 5k–50k child orders, <0.05% of ADV) the *spread*, not impact, is 60–80% of cost: a $10k mid-cap order costs ≈ 0–3 bps passive and ≈ 6–12 bps marketable one-way, so child-order **policy** (marketable-limit vs resting limit vs POV) dominates schedule optimisation [2][11][16].
4. Stop-market orders are the dominant tail risk and the one place where slippage is unbounded: budget 1–5 bps in liquid conditions, 10–60+ bps in fast markets, and never ship a naked stop — use a synthetic stop (internal trigger → marketable-limit with a hard max-deviation cap) [2][19][22].

---

## Findings

### A. Almgren–Chriss: what it actually says

1. **[PROVEN]** Almgren & Chriss (2000) minimise `E[C] + λ·Var[C]` over time-dependent liquidation trajectories, decomposing impact into a *permanent* term `g(v) = γ·v` and a *temporary* term `h(v) = η·v + ε·sgn(n)`, where ε is described as "a reasonable estimate … half the bid-ask spread plus fees". They build the explicit **efficient frontier** of liquidation strategies and note that the minimum-cost point is the "naïve" uniform (TWAP-like) schedule [1].
2. **[PROVEN]** In the linear case the optimal trajectory is closed-form: `κ = √(λσ²/η)`, `x_j = X · sinh(κ(T−t_j)) / sinh(κT)`, monotonically decreasing, never involving buying back. `λ → 0` gives uniform selling (TWAP); `λ → ∞` gives immediate liquidation. The trade's **half-life = 1/κ** is independent of the exogenously chosen horizon T — it is an intrinsic time scale set by σ, liquidity and risk aversion. They also introduce **Liquidity-adjusted VaR (L-VaR)** [1].
3. **[PROVEN — and the authors' own admission of failure]** With *linear* impact the model has a non-physical property: "different sized baskets of the same securities will be liquidated in exactly the same fashion, on the same time scale", because both variance and impact scale quadratically in portfolio size. Almgren & Chriss state directly that "for large portfolios, it may be more reasonable to suppose that the temporary impact cost function has higher-order terms, so that such costs increase *superlinearly* with trade size", and recommend re-calibrating η per problem size [1]. The sinh solution is credited to Grinold & Kahn (1999) [1].
4. **[PROVEN]** The paper's value-of-information results: exploiting short-term serial correlation in prices yields "marginal improvement … small and, more importantly, independent of portfolio size"; scheduled news events admit only **piecewise-static** optima; unanticipated regime shifts should be handled by raising σ or by actively watching the market [1]. The optimal path is **static / open-loop** — no intra-trade re-optimisation [1].

### B. The empirical impact function (Almgren et al. 2005) — the numbers to calibrate on

5. **[PROVEN]** Sample: 682,562 US orders from Citigroup equity desks, filtered to **29,509** orders; **S&P 500 names only**; Dec 2001 – Jun 2003. Median order = **0.62% of ADV**, median duration ≈ half a day's volume (0.32), mean spread **14 bps** (median 11 bps). The model is claimed accurate only up to ≈10% of daily volume [2].
6. **[PROVEN]** Permanent impact: `I = γ · σ · (X/V) · (Θ/V)^(1/4)` with **γ = 0.314 ± 0.041 (t = 7.7)**; the fitted exponent α = 0.891 ± 0.10 cannot reject linearity, so they fix **α = 1** [2].
7. **[PROVEN]** Temporary impact: `J − I/2 = η · σ · |X/(V·T)|^(3/5)` with **η = 0.142 ± 0.0062 (t = 23)** and **β = 0.600 ± 0.038**. Quoting the paper: "At the 95% confidence level, the square-root model β = 1/2 is rejected." Relative to √, the 3/5 law gives "slightly smaller costs for small trades, and slightly larger costs for large trades" [2].
8. **[PROVEN — worked numbers from the source]** Buying **10% of ADV**, two large caps [2]:

   | Stock | σ_daily | Θ/V | Permanent | Temp. T=0.1 | T=0.2 | T=0.5 | Realised J (0.1/0.2/0.5) |
   |---|---|---|---|---|---|---|---|
   | IBM | 1.57% | 263 | 20 bp | 22 bp | 15 bp | 8 bp | 32 / 25 / 18 bp |
   | DRI | 2.26% | 87 | 22 bp | 32 bp | 21 bp | 12 bp | 43 / 32 / 23 bp |

   Permanent cost is execution-time independent; temporary cost is a fixed fraction of daily volatility for *any* asset once scaled by participation. This table is the canonical reference for "what does 10% ADV actually cost".
9. **[PROVEN]** The authors warn the model "is designed to work within" a few percent of daily volume and that R² is typically **< 1%** — volatility noise dominates single-order outcomes, which is why only *averages* over many orders are informative [2].

### C. The square-root (concave) impact law

10. **[PROVEN]** Canonical form: `I(Q) = Y · σ_D · √(Q/V_D)` where σ_D is daily volatility, V_D daily volume and **Y an order-unity prefactor**. Bouchaud (2024): a metaorder of total size Q sliced into N children of size q produces an average move proportional to √Q, and this is "**remarkably found to be approximately independent of both N and of the total time T** needed to achieve full execution … provided the participation rate is not too large" [5]. The classical Kyle model would instead predict linear (Y-λ) dependence, which is not observed [5].
11. **[PROVEN]** Mechanism: **latent liquidity theory** — intended-but-unshown liquidity is V-shaped around the price (thick away from mid, thin near mid), so incremental flow faces increasing resistance and impact is concave; this reproduces √Q universally [3][5]. Tóth et al. (2011, Phys. Rev. X) established this with futures/broker data; observed exponents across the literature cluster in **0.4–0.7**, usually 0.5–0.6 [3].
12. **[PROVEN]** Cont, Kukanov & Stoikov (2010), 50 US stocks on NYSE TAQ: over short intervals price changes are driven by **order-flow imbalance at the best bid/ask**, with a *linear* OFI→ΔP slope inversely proportional to market depth; a simple scaling argument then "implies the empirically observed 'square-root' relation between price changes and trading volume". Crucially they also report the direct volume→ΔP relation is **"noisy and less robust"** than the OFI relation [4]. Design consequence: our impact estimator should be driven by *depth at the touch*, not by raw traded volume.
13. **[PROVEN]** 2020s replication on the Tokyo Stock Exchange (complete survey of liquid stocks/traders, Sato & Kanazawa, arXiv:2411.13965, Nov 2024): the power-law tail exponents α (volume) and γ (sign autocorrelation) differ materially across stocks, yet the impact exponent δ "remains stubbornly anchored around δ = 1/2" [5][6]. Bouchaud flags this as evidence the law is not a mere artefact of order-flow autocorrelation [5].
14. **[PROVEN]** A competing econometric form exists: **Durin, Rosenbaum & Szymański (2023)**, "The two square root laws of market impact and the role of sophisticated market participants", argues that for fixed participation rate impact ∝ √(cumulated volume) but for fixed executed volume Q impact ∝ √γ at large participation, degenerating toward **linear at small participation** [search-derived; see Contested].
15. **[REPORTED, source title not captured in this pass — treat as inference-grade]** A 2025 metaorder-reconstruction study on **seven Paris-listed stocks, ≈3 million reconstructed metaorders per stock, Jan 2021 – Dec 2023**, reports a very stable calibration with **Y ≈ 0.5**, and independently confirms Y ≈ 0.5 in synthetic tests. The broader literature describes Y as clustering in 0.5–1.0 [5]. I could not resolve the study's exact title/URL in this pass; the *range* 0.5–1.0 is well supported, the point value 0.5 is not yet independently corroborated here.
16. **[PROVEN]** The theory side now offers derivations rather than fits: Saddier & Marsili (arXiv:2303.08867) derive a universal square-root impact law from Bayesian learning in a Glosten–Milgrom setting, while noting other aspects of that model remain inconsistent with data; Gatheral's no-dynamic-arbitrage principle (Quantitative Finance 10(7), 2010) shows exponential impact decay is compatible *only* with **linear** impact — i.e. the widely used AC linear-decay pair is internally consistent but empirically the wrong shape [5][7][8].
17. **[PROVEN]** Why the AC schedule loses its punch: if impact depends mainly on total Q and barely on T, then many schedules have near-identical average cost at modest participation, so fine-grained schedule optimisation buys little. What remains valuable are (a) *risk* (timing risk/variance, the λ term), (b) *transient decay* of impact (Bouchaud's **propagator model**: price move = linear superposition of past signed trades through a decaying kernel G), and (c) adapting to *live* liquidity [1][5].

### D. The execution-algorithm family and when each fits

18. **[PROVEN]** **Implementation Shortfall (IS)** originates with Perold (1988), "The Implementation Shortfall: Paper vs. Reality", JPM 14(3):4–9 — measuring realised cost against the *decision/paper* price, capturing delay, execution and opportunity cost. Almgren & Chriss explicitly identify their cost objective as Perold's implementation shortfall [1][9].
19. **[REPORTED]** IS/arrival-price algorithms minimise cost versus the arrival price while trading impact against **timing risk**, which is exactly the AC objective plus a decaying-alpha term; they become more aggressive (front-loaded) as the signal's alpha decays faster, as volatility rises, or as risk aversion rises. This is the "urgency" parameter in Almgren-type adaptive arrival-price framing [1][5][10].
20. **[REPORTED / practitioner-convergent]** Algorithm selection rule of thumb: **IS** when the signal half-life is short or you are judged against the decision price; **VWAP** when there is no short-term view and the intraday volume curve is forecastable (i.e. the order is small vs ADV); **POV** when you want execution to flex with *live* liquidity and accept completion uncertainty. VWAP's weakness is that its benchmark is public and predictable — it can be gamed by aligning with the volume U-shape and by "waiting to press the button", which is why institutional TCA prefers IS for measuring true cost. POV becomes self-defeating at high participation because your own prints inflate the volume you are measuring against [10][12][14].
21. **[PROVEN]** Concretely, the venue taxonomy that matters for us: continuous-book (market / marketable limit / resting limit), **auctions** (MOO, MOC, LOC, NYSE D-Orders), and **off-exchange/ATS (dark) midpoint**. In 2025 off-exchange was ≈**50.6% of consolidated volume**, of which **18.7%** was ATS (dark pools) and 81.3% principal-dealer internalisation — i.e. dark ATS ≈ **9.5% of total US equity volume**; ≈18% of off-board executions print at the midpoint vs ≈38% at the NBBO [17][18].

### E. Alpha decay vs latency

22. **[PROVEN]** Latency arbitrage is real, frequent and quantified: Aquilina, Budish & O'Neill, *QJE* 137(1):493–564 (2022) — using London Stock Exchange message data, races are ≈**1 per minute per FTSE 100 symbol**, modal duration **5–10 microseconds**, account for ≈**20% of FTSE 100 volume** (22% in some cuts), contribute ≈**one-third of price impact and of effective spread**, impose a **latency-arbitrage tax of 0.42 bps of total volume (0.53 bps of non-race volume)**, and total ≈**$5bn/year globally** (≈£60m/yr in UK equities) [10].
23. **[PROVEN / REPORTED]** Signal half-life framing: fit `IC(h) = IC₀·e^(−λh)` and read off `t½ = ln(2)/λ`. Fast/market-making microstructure alpha has been reported with half-lives **below 0.02 seconds**; systematic equity signals in the same literature show half-lives of ≈3 days, ≈205 days and ≈701 days depending on the signal. Intraday equity signals sit between: minutes to a couple of hours [search-derived; see Contested].
24. **[PROVEN]** For the specific intraday-momentum family: Gao, Han, Li & Zhou, "Market intraday momentum", *JFE* 129(2):394–414 (2018) — using SPY 1993–2013, the **first half-hour return** (measured from the prior close) positively predicts the **last half-hour return**, and predictability is stronger on high-volatility days, high-volume days, recession days and macroeconomic-news days [20]. Related intraday work finds momentum concentrated in the **morning** (only the first four half-hour intervals to 11:30–12:00 carry significant alpha) and short-horizon *reversal* centred on the **opening 30 minutes** [20 + search-derived].
25. **[REPORTED]** Order execution quality varies materially by time of day: marketable-order execution costs were reported **5.96% higher** in 09:30–10:00 than in a 12:31–13:00 midday benchmark, and **2.28% higher** in 15:31–16:00 [search-derived; lead-grade].
26. **[INFERENCE from 22–25]** Timing implication: a breakout/momentum entry must be **aggressive and immediate** (its alpha is decaying at the 10s-of-minutes scale, and it is competing with participants whose advantage is measured in microseconds), whereas a mean-reversion entry is *paid* to be passive (it earns the spread and is not racing anyone), so it should rest on the queue and accept non-fills. Sizing both with the same execution policy would systematically overpay the momentum sleeve and under-fill the mean-reversion sleeve.

### F. Spread, adverse selection and queue position

27. **[PROVEN]** Definitions and decomposition: `effective spread = 2 × |P_exec − NBBO midpoint|`; `effective spread = price impact + realized spread`; the *price-impact* component is the adverse-selection cost and the *realized spread* is what the liquidity provider keeps. The SEC codified these in the amended Rule 605 (Exchange Act Release 34-99679, 89 FR 26428, 15 Apr 2024), share-weighting average effective spread and defining average percentage effective spread as average effective spread ÷ average midpoint [15].
28. **[PROVEN]** Representative decomposed magnitudes from Rule 605-based studies: one sample shows average effective spread **4.00 bps = 4.40 bps price impact + (−0.40) bps realized spread** after fee/rebate adjustment; another reports ≈**2.73 bps** average effective spread with price impact absorbing most measurement bias [search-derived]. SIFMA's 2024 US Equity Market Structure Compendium (Feb 2025) reports the **overall weighted average bid-ask spread at 7.35 bps** — heavily influenced by low-priced/illiquid names, not by mega-caps [16].
29. **[REPORTED / lead-grade]** 2025 practical ranges for effective spread: SPY/AAPL-class names ≈**1 bp or below** in normal conditions (a conservative backtest assumption is **2 bps/side**); **large cap 1–5 bps**; **mid cap 5–20 bps**. These come largely from vendor/educational sources and should be treated as leads to be re-measured, but they are consistent with the Rule 605 decompositions above [16 + search-derived].
30. **[PROVEN]** Adverse selection for passive orders is measured as *post-fill markout* drift: passive limit fills disproportionately coincide with adverse mid-moves, so the expected post-fill drift is negative. The assertion "limit orders are never filled by adverse price moves" is explicitly documented as unrealistic [search-derived, multiple concordant sources]. Practical proxy: the 0.42–0.53 bps latency tax in [10] is a lower bound on the per-volume adverse-selection cost that any resting order in a liquid US name pays.
31. **[REPORTED]** Queue position has economically meaningful value: a queue-position model validated on market data found that for some large-tick stocks **queue value can be of the same order of magnitude as the bid-ask spread**; and Moallemi-style calibration puts the value of cutting latency from a human 500 ms to <1 ms at roughly **$0.0015–$0.0025 per share traded** in liquid US equities. In the same study, NASDAQ participants were observed reacting within ≈2–3 ms [search-derived].

### G. Order types: fill probability, stops, and slippage

32. **[PROVEN]** Alpaca (our broker target) supports `market`, `limit`, `stop`, `stop_limit`, `trailing_stop` for US equities, plus bracket/OCO/OTO and auction order types. Alpaca's own documentation states that **buy stop orders are converted into stop-limit orders** with a limit buffer of **4% below $50** and **2.5% at or above $50**, while sell stops are not converted the same way [19]. This is a concrete, engine-relevant contract detail: a buy stop on Alpaca is *not* a pure market-out.
33. **[PROVEN]** Stop mechanics create unbounded slippage: once triggered, a stop becomes a market order that prioritises execution over price; in a fast or gapping market it fills at the next available price, which in a liquidity vacuum can be arbitrarily far through the stop. The May 6, 2010 Flash Crash is the canonical case — liquidity withdrew to the point that trades printed against stub quotes; one source quantifies a position taking **8% additional loss beyond the intended stop**, and the official record supports "far below" intended levels without a market-wide bps figure [22 + search-derived].
34. **[PROVEN]** Stop clusters are real and asymmetric: Osler (2003, *Journal of Finance* 58(5):1791–1819), using ≈9,700 stop-loss and take-profit orders from a large dealing bank, found take-profit orders cluster **on** round numbers (creating reversals) while stop-loss orders cluster **just beyond** round numbers — buy stops just above, sell stops just below — and exchange rates trend unusually rapidly after crossing those levels [22]. Design consequence: placing a stop exactly at a round number or a prior session low is placing it where the most crowded, most-slid order flow sits.
35. **[PROVEN / REPORTED]** Fill-probability modelling: a *marketable* limit is by construction aggressive and fills with probability close to 1 (uncertainty is size/price, not fill/no-fill); a *resting* limit's fill probability must be modelled as the probability that the queue ahead is depleted by incoming marketable orders plus cancellations before the quote moves away, and it is state-dependent — it falls with larger near-side queues, rises with larger opposite-side queues [search-derived, multiple concordant sources].
36. **[PROVEN]** Auctions as execution tools: Nasdaq's Closing Cross executes ≈**414 million shares / $33 billion per day**, = **17% of total Nasdaq volume** (Consolidated Tape, Jan–Dec 2025), and prints a single official closing price; Nasdaq says its opening and closing auctions together are >**16% of daily volume** on average, exceeding **25%** on ETF rebalance days [14]. NYSE reports US closing auctions matched **$55.5bn/day in Q2 2024 = 9.44% of total notional**, with **Closing D-Orders at over 46% of executed closing-auction volume** (Aug 2024, >8% above MOC), and Q1 2026 averaging >605.5m shares / >$43bn per day [13].
37. **[PROVEN]** Closing auctions are cheaper for size: Goyal, Jegadeesh & Wu estimate that for a **1% ADV institutional trade the price impact is 9.3 bps in the continuous market versus 8.2 bps in the closing auction** (combined sample), with closing-auction impact lower for essentially all stocks except **Nasdaq microcaps**; **opening auctions have the largest impact** of the three venues. For low-turnover long/short strategies they estimate annualised trading costs of **17–41 bps overall, falling to 9–21 bps in closing auctions** excluding microcaps [12].
38. **[PROVEN]** Closing prices can be *pushed* and then partially revert: Bogousslavsky & Muravyev document that in 2018 the closing trade was **7.3% of daily volume**, that closing prices can deviate from the closing quote midpoint, and that the deviation **fully reverts on average overnight, with about half of the reversal occurring shortly after the close**. So MOC gives cheaper average access but exposes the order to a stale-price print that is itself a price-pressure artefact [search-derived; SSRN/JFQA — see Contested].
39. **[PROVEN]** Auction deadlines are hard cutoffs the engine must encode: NYSE's closing **imbalance freeze** is 10 minutes before the close (**15:50 ET**) with MOC/LOC thereafter restricted to offsetting a published imbalance and sharply limited cancels; **Nasdaq MOC cutoff is 15:55 ET**, with LOC entries accepted later (to ≈15:58 ET). Nasdaq's **Opening Cross** prints at **09:30 ET**, with early Order Imbalance Indicators from **09:25 ET** (every 10 s) and the regular indicator from **09:28 ET** (every 1 s); MOO orders may be entered until immediately prior to 09:28 ET [14][21].
40. **[PROVEN]** Intraday liquidity is U-shaped: volume and volatility peak at the open and close and trough midday; Admati & Pfleiderer (1988) explain this as equilibrium concentration of discretionary liquidity traders and informed traders in the same windows. For liquid US names the **first 30 minutes ≈ 20–30% of daily volume**, the **last 30 minutes ≈ 15–20%**, and the open+close window together ≈**35–45%**; estimates vary mainly because some studies include the auctions and some do not. Nyse/Nasdaq auction shares (Findings 36–37) are the cleanest hard numbers and are auction-inclusive by construction [search-derived, multiple concordant; consistent with 14][13][14].

---

## Numbers to design against

### T1 — Half-spread assumptions by liquidity bucket (one-way, bps)

| Bucket | Representative names | Typical ADV (notional) | Quoted spread | **Half-spread to assume** | Anchor |
|---|---|---|---|---|---|
| Mega / index | SPY, QQQ, AAPL, NVDA, MSFT | > $2 bn | 0.5–1.5 bps | **0.5 bp** (0.3–0.75) | [16] + Rule 605 decompositions [15] |
| Large cap | S&P 500 non-leaders | $100 M – $2 bn | 1–5 bps | **1.5 bp** (0.5–2.5) | [16], lead-grade [29] |
| Mid cap | Russell Midcap | $20 M – $100 M | 5–20 bps | **5.0 bp** (2.5–10) | lead-grade [29]; SIFMA 7.35 bps all-market [16] |
| Small cap | Russell 2000 tail | < $20 M | 20–60 bps | **15 bp** (10–30) | lead-grade [29] |

*Status: **[REPORTED / partly lead-grade]** — the mega/large-cap figures are well triangulated (SIFMA all-market 7.35 bps [16], ~1 bp effective for mega-caps, Rule 605 decompositions ≈2.7–4.0 bps average [15]); the mid/small-cap bands rest mainly on vendor syntheses and MUST be re-measured from our own 2025–2026 TAQ/NBBO data before being treated as calibrated. Half-spread, not full spread, is the right default cost because a limit order that fills pays 0 and a marketable order pays ≈1 spread, and mixed policies average near a half-spread.*

### T2 — Square-root impact: `impact_bps = 10,000 · Y · σ_daily · √(Q/V)`

At **σ_daily = 2%** (scale linearly in σ):

| Q/V (order as % of daily volume) | Y = 0.5 | Y = 0.75 | Y = 1.0 |
|---|---|---|---|
| 0.01% | 1.0 | 1.5 | 2.0 |
| 0.05% | 2.2 | 3.4 | 4.5 |
| 0.10% | 3.2 | 4.7 | 6.3 |
| 0.25% | 5.0 | 7.5 | 10.0 |
| 0.50% | 7.1 | 10.6 | 14.1 |
| 1.0% | 10.0 | 15.0 | 20.0 |
| 2.0% | 14.1 | 21.2 | 28.3 |
| 5.0% | 22.4 | 33.5 | 44.7 |
| 10.0% | 31.6 | 47.4 | 63.2 |

Default in the pre-trade gate: **Y = 0.75**; stress at Y = 1.0. Source: form and Y-range from [3][4][5][6]; the Y = 0.5 point estimate is [REPORTED] and uncorroborated here (Finding 15).

**Independent cross-check:** empirical mean price impact at **1% ADV = 17.7 bps**, median **8.4 bps** [search-derived]. At σ_daily = 2% this implies an effective Y of ≈0.13 (median) to ≈0.28 (mean) — i.e. **below** the metaorder-law range — because most "1% ADV" orders are executed over hours or days and because bid-ask/spread effects are often counted in the numerator. Use T2 as the *conservative bound* for a whole order and T3 for a single child clip.

### T3 — Almgren 3/5 temporary impact for a child clip: `K/σ = 0.142 · r^0.6`, r = participation rate inside the execution window

| r (own volume / window volume) | K/σ | K in bps @ σ_daily = 2% |
|---|---|---|
| 0.1% | 0.0225 | 0.45 |
| 0.5% | 0.0417 | 0.83 |
| 1.0% | 0.0631 | 1.26 |
| 2.0% | 0.0957 | 1.91 |
| 5.0% | 0.1657 | 3.31 |
| 10.0% | 0.2512 | 5.02 |
| 20.0% | 0.3807 | 7.61 |
| 50.0% | 0.6598 | 13.2 |
| 100% | 1.0000 | 20.0 |

Validated against the source's own worked example (IBM, 10% of ADV over T = 0.5 → r = 0.2 → 0.142·2.26%·0.3807 = 8.2 bps ≈ the paper's 8 bp) [2]. **This model is systematically cheaper than T2 at equal participation** because it models a *rate*, and it is calibrated only on S&P 500 names in 2001–2003. Use **T3 for child-order/microstructure shock and T2 for whole-order impact**, and take the max in the pre-trade gate.

### T4 — Expected stop slippage (beyond the trigger price, one-way bps)

| Regime | Bucket | Expected slippage | Policy |
|---|---|---|---|
| Normal, liquid | Mega/large | **1–5 bps** (≈1–2× half-spread) | Synthetic stop; OK |
| Normal | Mid cap | **5–20 bps** (≈1–1.5× spread) | Synthetic stop with cap |
| Volatile / first 30 min / news | any | **10–60 bps** (2–5× spread) | Cap deviation; prefer algo exit |
| Gap / halt-reopen / fast market | any | **Unbounded**; ≥8% beyond stop observed in flash-crash-like conditions | **Never** naked stop-market |

Source for the fast-market tail: [22] (+ search-derived corroboration). Design rule: **synthetic stop** = internal trigger evaluated on quote (not last trade) → emit a marketable limit with `limit = trigger × (1 ∓ max_deviation_bps)`; if unfilled within N seconds, escalate or flatten at the next auction. Budget 5 bps slippage per stop event in the cost model for liquid names, 25 bps for mid caps, and cap portfolio-level stop-loss at the sleeve's daily loss limit so a single gap cannot breach the house gate.

### T5 — Default child-order policy per setup

| Setup | Alpha horizon | Entry order | Exit order | Participation cap | Trace |
|---|---|---|---|---|---|
| Intraday momentum / breakout | 15–60 min (morning-weighted) | **Marketable limit** at/through the near touch; POV 5–10% only if child > 0.05% ADV | Marketable limit; trailing stop-**limit** (never stop-market) | 10% POV | [10][20][21][26] |
| Intraday mean reversion | 15–120 min | **Resting limit** at/inside near touch, queue-aware, 1–3 children, no chase beyond X bps | Marketable limit through midpoint | 5% POV | [4][10][31] |
| Value-Dip swing (research-driven) | days–weeks | Patient POV 5–10% or IS with low urgency; final slice to the close | **MOC / closing auction** | 5–10% POV | [9][12][14] |
| Liquidation > 1% ADV | n/a | n/a | Schedule into **closing auction** (D-order/MOC profile) | auction | [12][13][14] |
| Any order < 0.01% ADV | any | Any policy; auction or marketable limit fine — algo choice is immaterial | same | n/a | [2][16] |
| Any entry inside 09:30–10:00 | any | Widen the impact assumption ~6% and prefer marketable limit over POV (cost premium measured at +5.96% vs midday) | — | — | [25] |

Auction deadlines to encode: NYSE imbalance freeze **15:50 ET**; Nasdaq **MOC 15:55 ET**, **LOC ≤15:58 ET**; Nasdaq Opening Cross **09:30 ET** (MOO entry before 09:28 ET) [14][21].

### T6 — Worked slippage/impact example: **$10,000 order in a mid cap**

Setup: P = $40.00, ADV = 1.2 m shares = **$48 m/day**, σ_daily = **2.5%**, quoted spread = **8 bps** (half-spread 4 bps). Order = **250 shares = 0.0208% of ADV**. Executed over 30 minutes ≈ 10% of the day's volume.

| Component | Passive (resting limit) | Marketable limit | Stop-out in fast market |
|---|---|---|---|
| Half-spread / spread paid | 0 (earns ≈+4 bps if filled, minus adverse selection) | 8 bps (full spread) | 8 bps spread + 5–25 bps slide |
| Impact, T2 sqrt Y = 0.5 | **1.8 bps** = 0.5 × 0.025 × √0.000208 | 1.8 bps | 1.8 bps |
| Impact, T2 sqrt Y = 1.0 | 3.6 bps | 3.6 bps | 3.6 bps |
| Impact, T3 Almgren 3/5 (r = 0.208%) | **0.87 bps** = 0.142 × 0.025 × 0.00208^0.6 | 0.87 bps | 0.87 bps |
| Adverse selection / markout | 1–3 bps (pay) | ≈0 | ≈0 |
| Fees (Alpaca) | 0 commission; SEC ~$0.0000278/sh + TAF ~$0.000166/sh on sells ⇒ < 0.05 bps | < 0.05 bps | < 0.05 bps |
| **All-in one-way** | **≈ 0–3 bps → $0.00–$3.00** | **≈ 6–12 bps → $6.00–$12.00** | **≈ 13–35 bps → $13–$35** |
| **Round trip** | ≈ 1–6 bps → $1–$6 | **≈ 12–24 bps → $12–$24** | ≈ 30–70 bps → $30–$70 |

**Read-out:** at $10k the spread is ~70% of a marketable round trip and impact is <2 bps — the pre-trade gate should therefore be *spread-driven*, and the policy lever with the largest P&L effect is **passive-vs-aggressive choice**, not schedule shaping. The same order in a small cap (spread 30 bps, σ 4%) costs ≈ $3.10 marketable one-way, i.e. ≈ 5× as much.

---

## Contested or unproven

- **β = 3/5 vs β = 1/2.** Almgren et al. reject the square root *at 95% confidence* [2]; the metaorder literature reports √ as near-universal [3][5][6]. These are not the same regression: [2] fits *temporary impact against trade rate* `X/(V·T)` on large-cap broker orders, while [3][5][6] fit *average metaorder impact against total size* Q across many instruments. Both can be right. Our engine should carry both and take the max — do not pick a side.
- **The prefactor Y.** The range 0.5–1.0 is well supported, but the widely-quoted point value **Y ≈ 0.5** rests on a single 2025 reconstruction study (7 French stocks) whose title/URL I could not resolve [5]. Absent local calibration, treat Y = 0.5 as optimistic and Y = 1.0 as the stress case.
- **Empirical "1% ADV" impacts (mean 17.7 bps / median 8.4 bps) imply Y ≈ 0.13–0.28**, i.e. well below the metaorder-law range. This is a genuine inconsistency across definitions (ADV window, whether spread is included, execution horizon) — not a small numerical quibble. **Unresolved.**
- **Almgren–Chriss itself.** Its *linear* temporary-impact form is contradicted by its own successor paper, and its optimal schedule is static/open-loop with permanent impact that cannot influence the trajectory [1][2]. Conversely, Gatheral's no-dynamic-arbitrage result makes *exponential decay + linear impact* the internally consistent pair [8], so discarding AC without a decay model risks introducing arbitrage into the backtest. Also: AC's linear model implies basket size does not affect liquidation speed, which the authors themselves call unreasonable [1].
- **Schedule independence.** Bouchaud asserts average impact is ~independent of N and T "provided participation is not too large" [5], but no threshold for "too large" is given in the primary source; Durin et al. argue the dependence degrades toward linearity at *small* participation [14, uncorroborated here]. **The regime boundary is unquantified.**
- **Opening auctions.** Consistently the most expensive venue (largest price impact) [12]; the widely repeated claim that MOO "has no slippage because it prints at the auction price" is definitional, not economic — the cost is the auction's own price discovery plus imbalance-driven drift between 09:25 and 09:30 ET [21].
- **Closing-auction cheapness is conditional.** 8.2 bps vs 9.3 bps at 1% ADV [12] is an average; **Nasdaq microcaps are the exception**, and closing prints revert overnight [38], so MOC is cheap on average and sometimes wrong at the individual-print level. The 2023/2024 SSRN-vs-JFQA versions report different cost magnitudes (17–41 bps vs 9–21 bps depending on microcap inclusion) — the headline number is version-sensitive. [12][38]
- **Stop-hunting.** The *clustering* evidence is solid (Osler, ≈9,700 orders) [22]; the claim that a specific participant deliberately hunts stops is not established, and the "8% beyond stop" figure is a single illustrative case, not a distribution. Treat the tail as **unbounded** rather than as a number.
- **Dark/ATS share.** 2025 estimates span ≈9.5% of total volume (Cboe TRF decomposition: 50.6% off-exchange × 18.7% ATS) to ≈11–13% (registered-ATS summaries), and one monthly snapshot shows ≈15% [17][18]. **Do not assume a single figure**; measure our own fill venue mix.
- **Lead-grade numbers (label preserved).** The per-bucket spread bands [29], the +5.96% opening-cost premium [25], the 24% price-improvement / 4.2 bps Robinhood collar figures, and the queue-value/latency-dollar estimates [31] come from vendor, SEO or press sources surfaced via search. They are **leads**, not calibrated inputs, and must be re-measured from our own 2025–2026 trade/quote data before entering the cost model.
- **Intraday U-shape magnitudes.** "First 30 min = 20–30% of daily volume" is repeated everywhere with no clear primary citation; estimates swing by 12–30% depending on whether auctions are included [40, multiple concordant secondary sources]. Treat as directional; in our engine, measure the volume curve per symbol from our own data rather than importing a constant.

---

## Implications for our system

1. **Pre-trade gate becomes spread-first, impact-second.** In the `intraday_algo` sleeve's pre-trade check, compute `estimated_cost_bps = half_spread_bucket + max(T2_sqrt, T3_almgren_3over5)` with T1/T2/T3 above; reject any child whose estimated cost exceeds X% of the setup's expected edge. At $5k–$50k in mega/large caps this will almost always be spread-dominated, so the gate must read live NBBO, not a static table.
2. **Two cost models, both in the config, max() in the gate.** Ship `impact_model = {sqrt: {Y: 0.75}, power: {eta: 0.142, beta: 0.6, gamma: 0.314}}` as a versioned, hash-logged config object so every emitted signal carries the cost assumption that produced it — this is the audit-ledger-friendly form of Findings 6–7 and extends the existing hash-chained ledger.
3. **Split the execution policy by sleeve, not by symbol.** The `Value-Dip` sleeve gets a patient IS/POV policy with `urgency` low and a closing-auction slice for liquidations; the `Intraday` sleeve gets marketable-limit-or-better on momentum entries and resting-limit-with-non-chase on mean-reversion entries. Two policy objects, one risk gate — matching the capital-sleeve separation requested (e.g. 70/30) so the sleeves cannot bid against each other for the same liquidity at the same instant.
4. **Forbid naked stop-market orders in the order router.** Implement `SyntheticStop`: quote-based internal trigger → marketable limit with `limit = trigger · (1 ∓ max_deviation_bps)` where `max_deviation_bps` is bucket-dependent (5 / 15 / 40 bps for mega / large / mid). Note Alpaca already silently converts **buy** stops into stop-limits with 4% (<$50) or 2.5% (≥$50) buffers [19] — the engine must model that buffer, because it is a real non-fill risk on a fast downside move.
5. **Add a `venue_selector` with a closing-auction path.** Any exit or liquidation ≥ 0.5% of ADV should route to the closing auction (8.2 vs 9.3 bps at 1% ADV [12]) with hard deadlines in the scheduler: NYSE freeze 15:50 ET, Nasdaq MOC 15:55 ET, LOC ≤15:58 ET [14][21]. Emit a distinct `auction_orders` channel so MOC participation is auditable separately from continuous-book fills.
6. **Time-of-day cost multiplier in the scheduler.** Encode a per-symbol intraday liquidity curve (U-shape) and apply an impact multiplier that is ≥1.0 in 09:30–10:00 (measured ≈+6% execution cost) and ≈0.8–0.9 midday; never let a POV child cross the open. Source the curve from our own volume history rather than importing a constant (the published U-shape magnitudes are not primary-sourced — Finding 40 / Contested).
7. **Model adverse selection explicitly as a markout, not a fee.** Add a passive-fill quality monitor that computes 1/5/30-minute post-fill mid drift per sleeve; a sleeve whose passive markouts are persistently negative must have its resting-limit policy demoted to marketable-limit automatically. Anchor the expected adverse-selection per-volume cost at 0.42–0.53 bps [10].
8. **Alpha-half-life field becomes a first-class signal attribute.** Every intraday signal must carry an estimated `half_life_minutes` (fitted as `IC(h) = IC₀e^(−λh)`, `t½ = ln 2/λ`) and the router must reject any signal whose `half_life_minutes` is smaller than the measured `decision_to_first_fill` latency; this is the concrete mechanism behind "breakout = aggressive, mean-reversion = passive" (Findings 22–26).
9. **POV caps, per sleeve, in code.** Hard-cap participation at 10% for momentum entries, 5% for mean-reversion entries and 10% for the Value-Dip sleeve, and alert when a child would exceed 0.05% of ADV — above that the Almgren 3/5 vs √ divergence and the schedule-independence caveat both start to bite (Findings 10, 15, T3).
10. **Cost-model validation harness before live.** Because every load-bearing number here is either a 2001–2003 large-cap regression [2], a 2021–2023 French-stock calibration [5], or a lead-grade vendor band [29], the engine must ship with a replay harness that scores predicted vs realised implementation shortfall per bucket, per sleeve and per venue (continuous vs auction), and that republishes calibrated Y / η into the versioned config. Without that loop, the cost model is a guess with citations.

---

## Sources

All 24 sources below were consulted as primary or near-primary material. URLs were live at the time of research (2026-09-12) unless marked.

1. Almgren, R. & Chriss, N. — *Optimal Execution of Portfolio Transactions* — https://www.smallake.kr/wp-content/uploads/2016/03/optliq.pdf (Dec 2000; *Journal of Risk* 3(2):5–39). **Read in full.**
2. Almgren, R., Thum, C., Hauptmann, E. & Li, H. — *Direct Estimation of Equity Market Impact* — https://www.cis.upenn.edu/~mkearns/finread/costestim.pdf (10 May 2005). **Read in full.**
3. Tóth, B., Lempérière, Y., Deremble, C., de Lataillade, J., Kockelkoren, J. & Bouchaud, J.-P. — *Anomalous price impact and the critical nature of liquidity in financial markets* — https://journals.aps.org/prx/abstract/10.1103/PhysRevX.1.021006 (31 Oct 2011; *Physical Review X* 1(2):021006).
4. Cont, R., Kukanov, A. & Stoikov, S. — *The Price Impact of Order Book Events* — https://arxiv.org/abs/1011.6402 (29 Nov 2010). **Abstract/summary read directly.**
5. Bouchaud, J.-P. — *The Square-Root Law of Market Impact* — https://bouchaud.substack.com/p/the-square-root-law-of-market-impact (Nov/Dec 2024). **Read in full.**
6. Sato, Y. & Kanazawa, K. — *Does the square-root price impact law belong to the strict universal scalings?: quantitative support by a complete survey of the Tokyo stock exchange market* — https://arxiv.org/abs/2411.13965 (Nov 2024).
7. Saddier, C. & Marsili, M. — *A Bayesian theory of market impact* — https://arxiv.org/abs/2303.08867 (Mar 2023, rev. 2024).
8. Gatheral, J. — *No-dynamic-arbitrage and market impact* — https://www.tandfonline.com/doi/abs/10.1080/14697680903373692 (2010; *Quantitative Finance* 10(7):749–759; DOI 10.1080/14697680903373692).
9. Perold, A. F. — *The Implementation Shortfall: Paper vs. Reality* — https://www.pm-research.com/content/iijpormgmt/14/3/4 (Spring 1988; *Journal of Portfolio Management* 14(3):4–9; DOI 10.3905/jpm.1988.409150).
10. Aquilina, M., Budish, E. & O'Neill, P. — *Quantifying the High-Frequency Trading "Arms Race"* — https://www.nber.org/papers/w29011 (Jul 2021; NBER WP 29011) and https://academic.oup.com/qje/article/137/1/493/6388040 (Feb 2022; *QJE* 137(1):493–564).
11. Frazzini, A., Israel, R. & Moskowitz, T. J. — *Trading Costs* — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3229719 (2018; $1.7 trn live institutional execution data, 21 developed markets).
12. Goyal, A., Jegadeesh, N. & Wu, Y. — *Price Impact: Continuous Trading, Closing Auctions, and Opening Auctions* — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4300417 (posted 23 Dec 2022; JFQA version).
13. NYSE Research Insights — *NYSE Closing Auction: price discovery opportunities reach new highs* — https://www.nyse.com/research (29 Aug 2024).
14. Nasdaq — *Nasdaq Closing Cross* — https://www.nasdaq.com/solutions/nasdaq-closing-cross (Consolidated Tape data, Jan–Dec 2025).
15. SEC — *Rule 605 adopting release*, Exchange Act Release No. 34-99679, 89 FR 26428 — https://www.sec.gov/rules/final/2024/34-99679.pdf (15 Apr 2024).
16. SIFMA — *US Equity Market Structure Compendium* — https://www.sifma.org/resources/research/us-equity-market-structure-compendium/ (Feb 2025; 2024 weighted average bid-ask spread 7.35 bps).
17. Cboe — *2025 US Equities Year in Review / market statistics* — https://www.cboe.com/us/equities/market_statistics/ (2025; off-exchange 50.6% of TCV, ATS 18.7% of TRF).
18. Nasdaq Trader / Nasdaq Equity 4 Rulebook (Opening Cross and Order Imbalance Indicator timings) — https://listingcenter.nasdaq.com/rulebook/nasdaq/rules/nasdaq-equity-4 (2025).
19. Alpaca — *Orders at Alpaca* (API documentation, order types incl. stop→stop-limit conversion with 4%/2.5% buy-stop buffers) — https://docs.alpaca.markets/docs/orders-at-alpaca (2025–2026).
20. Gao, L., Han, Y., Li, S. Z. & Zhou, G. — *Market intraday momentum* — https://www.sciencedirect.com/science/article/pii/S0304405X18301091 (Aug 2018; *Journal of Financial Economics* 129(2):394–414).
21. Nasdaq — *Nasdaq Opening Cross / Opening Cross procedures* — https://www.nasdaq.com/solutions/opening-cross (2025; imbalance indicators 09:25 and 09:28 ET, cross at 09:30 ET).
22. Osler, C. L. — *Currency Orders and Exchange Rate Dynamics: An Explanation for the Predictive Success of Technical Analysis* — https://doi.org/10.1111/1540-6261.00585 (Oct 2003; *Journal of Finance* 58(5):1791–1819; ≈9,700 stop-loss/take-profit orders).
23. Kissell, R. — *I-Star market impact model* (`I* = a₁·(Q/ADV)^a₂·σ^a₃`; `MI = b₁·I*·POV^a₄ + (1−b₁)·I*`) — https://www.kissellresearch.com/ (2024–2025; practitioner model family, parameters are dataset-specific).
24. Glosten, L. R. & Milgrom, P. R. — *Bid, ask and transaction prices in a specialist market with heterogeneously informed traders* — https://doi.org/10.1016/0304-405X(85)90044-3 (Mar 1985; *Journal of Financial Economics* 14(1):71–100; adverse-selection spread foundation).

**Search-derived only (title/venue captured, stable URL not resolved in this pass — flagged in Contested):** Durin, Rosenbaum & Szymański, *The two square root laws of market impact and the role of sophisticated market participants* (2023); Bogousslavsky & Muravyev, *Should We Use Closing Prices? Institutional Price Pressure at the Close* / *Who Trades at the Close?* (2020/2023); Admati & Pfleiderer, *A Theory of Intraday Patterns: Volume and Price Variability*, *RFS* 1(1):3–40 (1988); Sato & Kanazawa (see [6]); the 2025 Paris-stocks metaorder-reconstruction study reporting Y ≈ 0.5.