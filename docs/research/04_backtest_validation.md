# Backtest validation and statistical inference for intraday strategies

**Bottom line (4 lines).**
1. A raw annualized Sharpe is close to meaningless at intraday trade counts: with zero true skill, N independent trials already produce E[max SR] ≈ sd(SR_trials)·maxZ(N), so 500 trials inflate the best backtest to an annualized Sharpe ≈1.5 before you have discovered anything [1].
2. Gate on **Deflated Sharpe Ratio (DSR) ≥ 0.95 AND PBO ≤ 0.05**, and **log the number of trials N**; a backtest that does not disclose N is unfalsifiable and must be rejected regardless of how good it looks [1][2].
3. Comparing a 0.9-Sharpe swing sleeve to a 1.6-Sharpe intraday sleeve over 2026-01-02→2026-09-11 (~175 sessions) is statistically hopeless: with ρ=0.15 the ΔSR t-stat is −0.45, and t=2 needs ~3,500 sessions (~14 years) [15][16].
4. Cost-inclusive validation must assume **≥10 bps round-trip for liquid US large caps (20 bps conservative)** plus borrow; at 1,000 trips/year that is 100% of capital per year in cost, so an 8 bps/trip gross edge nets ≈ −20%/year [20][21].

---

## Findings

### A. The multiple-testing / backtest-overfitting canon

1. **The Sharpe ratio is linearly linked to a t-statistic, so it inherits the multiple-testing problem.** Harvey & Liu show t = μ̂/(σ̂/√T) and SR = t/√T, i.e. t = SR·√T with T the number of observations [7, Eq. 1–2]. Consequence: a t of 3.0 over 20 years of *monthly* data is SR_annual ≈ 3.0/√20 = 0.67; conversely SR 2.0 over 1 year is only t=2.0 [7][17].

2. **The probability of backtest overfitting (PBO) is formally defined and estimated by combinatorially symmetric cross-validation (CSCV).** Split the (T×N) matrix of trial performance series into S equal, contiguous blocks (S even); form all C(S, S/2) balanced IS/OOS splits; in each split pick the IS-best configuration and record its OOS relative rank ω_c; define logit λ_c = ln(ω_c/(1−ω_c)). **PBO is the fraction of splits where λ_c < 0** (IS winner lands below the OOS median) [2, Def. 2.2, §2.2].

3. **CSCV calibration numbers.** S=16 gives 12,780 logits with sd of the PBO estimate < 0.0045 (< 0.01 estimation error at 95%); S=16 over ~4 years of daily data = quarterly partitions, preserving daily/weekly/monthly effects; S=24 over >6 years gives 2,704,156 logits [2, §4]. Bailey et al. propose the **customary rejection rule PBO > 0.05 [2, §3.1]**.

4. **Reported PBO outcomes.** In their overfit example, 100% of in-sample Sharpes were positive (range 1–3) while ~78% of OOS Sharpes were negative and **PBO = 74%**; in their real-strategy example OOS probability of loss was ~3% and **PBO = 0.04%** [2, §3.1–3.2]. This demonstrates that a high in-sample Sharpe tells you nothing about representativeness — the higher the IS Sharpe, the lower the OOS Sharpe in their example [2].

5. **Expected-maximum-Sharpe threshold (the DSR's engine).** For N independent trials with SR estimates ~ Normal(mean, var), the expected maximum is approximated by
   SR₀ = √Var(SR_trials) · [ (1−γ)·Φ⁻¹(1−1/N) + γ·Φ⁻¹(1−1/(N·e)) ]   (Eq. 1)
   where γ ≈ 0.5772 (Euler–Mascheroni), Φ⁻¹ the inverse standard-normal CDF, e Euler's number [1, Eq. 1].

6. **Deflated Sharpe Ratio (DSR).** DSR = PSR evaluated at SR₀:
   DSR = Φ( (SR̂ − SR₀)·√(T−1) / √(1 − γ̂₃·SR̂ + ((γ̂₄−1)/4)·SR̂²) )   (Eq. 2)
   where SR̂ is the observed Sharpe, T the sample length, γ̂₃ skewness and γ̂₄ (raw) kurtosis of the *strategy's returns in the same period units as T* [1, Eq. 2]. DSR corrects jointly for (i) selection bias from multiple trials (via SR₀) and (ii) non-normal returns (via γ₃, γ₄) [1].

7. **Probabilistic Sharpe Ratio and Minimum Track Record Length.** PSR(SR*) = Φ( (SR̂−SR*)·√(n−1) / √(1 − γ̂₃SR̂ + ((γ̂₄−1)/4)SR̂²) ); MinTRL = 1 + (1 − γ̂₃SR̂ + ((γ̂₄−1)/4)SR̂²)·(z_{1−α}/(SR̂−SR*))² [5]. Bailey & López de Prado's own example: under daily i.i.d. normal returns, a track record of **2.73 years** is required for an annualized SR of 2 to be established as greater than 1 at 95% confidence [5].

8. **Minimum Backtest Length.** MinBTL = (E[max_N]/SR_IS)² years, with the usable upper bound **MinBTL ≈ 2·ln(N)/SR_IS²** years [3]. Worked: N=100 trials, SR_IS=1.0 → MinBTL ≈ 2·ln(100)/1 ≈ **9.2 years** [3][18].

9. **The 7-trials result.** Bailey, Borwein, López de Prado & Zhu show that with **only 7 independent configurations** you expect at least one **2-year** backtest with in-sample annualized SR > 1.0 while the expected OOS Sharpe is zero [3][7]. This is the canonical proof that holdout and short samples do not protect against overfitting [1][3].

10. **Harvey–Liu–Zhu: the t > 3.0 hurdle.** Surveying 313 articles / 316 factors (published count), HLZ conclude a new factor "needs to clear a much higher hurdle, with a t-statistic greater than 3.0" [6, abstract]. They also report stricter, method-dependent cutoffs — **≈3.78** for ~300 tests under Bonferroni-type control and **≈3.9** under alternative dependence assumptions — and note the estimated number of tests exceeds the 316 published factors because unpublished attempts are unobserved [6][8][18]. The traditional |t| > 2.0 is stated to be too low [6].

11. **Harvey–Liu "haircut" Sharpe ratio (usable in practice).** Transform SR → t → p^S; adjust for N tests (independence case: p^M = 1 − (1 − p^S)^N); convert back: the haircut is the % gap between raw and adjusted SR [7]. Their headline numbers: **T=240 months, SR=0.75, N=200 → p^S=0.0008 → p^M=0.15 → adjusted annual SR 0.32 (≈ −57%)**; and across Sharpe levels, **the haircut is >50% for SR < 0.4 but ≤25% for SR > 1.0** — i.e. the industry's blunt 50% haircut is too lenient at the margin and too harsh for exceptional Sharpes [7, Exhibits 1–3]. Their worked program example: SR 1.0 annualized over 120 months, 100 tests, average correlation 0.4 → **autocorrelation-corrected SR 0.912 → BHY-adjusted 0.438, haircut 52.0%** [7, Exhibit 5].

12. **Harvey–Liu minimum profitability hurdle (cost of multiple testing in return space).** With 300 tests, 240 monthly observations, 10% annual volatility, the minimum average monthly return is **0.365% (4.4%/yr) under a single test vs 0.616% (7.4%/yr) under BHY** [7, Exhibit 4].

13. **White's Reality Check, Hansen's SPA, Romano–Wolf stepdown — what each answers.** White (2000) tests whether the *best* rule in a frozen candidate set beats a benchmark, using the **stationary bootstrap** on the max statistic [9][10]. Hansen (2005) studentizes the loss differential (divides by its long-run sd) and uses a **sample-dependent recentred null**, giving a *consistent* p-value that is more powerful than WRC when the candidate pool contains many bad alternatives [11]. Romano–Wolf (2005) is a bootstrap **stepdown** that controls FWER under dependence by comparing the k-th ordered statistic against the bootstrap max over the *remaining* hypotheses, yielding monotone adjusted p-values [12].

14. **The classic empirical application.** Sullivan, Timmermann & White (1999) expand Brock–Lakonishok–LeBaron's 26 rules, test them over ~100 years of daily DJIA, and use the Reality Check to quantify data-snooping bias across the rule universe [10]. Later work showed profitability depends on market, sample and — critically — transaction costs [10].

### B. Cross-validation, walk-forward and intraday leakage

15. **Purged k-fold + embargo.** Financial labels are intervals, not instants. Purge every training row whose label span intersects the test-label span (train start inside test, end inside test, or fully containing test); then apply an **embargo** of a buffer immediately after each test fold to handle leakage through autocorrelated features or delayed market effects that survive label non-overlap [17].

16. **Combinatorial Purged CV (CPCV).** Split data into N contiguous groups, use k groups as test, giving **C(N,k)** purged/embargoed train/test folds, then recombine the fold results into a *distribution of OOS backtest paths* rather than one path; this distribution feeds PBO and a CI on OOS Sharpe [17]. Secondary sources agree on the core construction (path-count formula varies with notation) [17].

17. **Why holdout and plain k-fold fail here.** Holdout assesses generality as if a single trial occurred; at a 95% level, roughly **20 applications of holdout make false positives expected** rather than unlikely [1]. Plain k-fold on overlapping-label financial data leaks; leave-one-out is degenerate because no reliable performance metric can be computed on one observation [2, §4].

18. **Any OOS split is not truly out-of-sample if the researcher saw the data.** Harvey & Liu state there is no true historical OOS once the researcher knows the economy; also holdout forces a type-I/type-II trade-off in data splitting (long IS = powerful selection but short OOS = weak discrimination; 90/10 splits keep more true discoveries alive but leave too little to discriminate) [7].

19. **Walk-forward: anchored vs rolling.** Anchored (fixed start, expanding) tests whether parameters stay sensible with accumulating history and is more stable; rolling (fixed-length, sliding) tests whether only the recent regime matters and is more adaptive but noisier [19]. The dominant hazard is **re-optimization leakage**: tuning window lengths until the walk-forward curve looks good, repeatedly reusing the same "OOS" periods, and fitting scalers/PCA/feature selection on the full dataset before splitting [19].

20. **Intraday-specific leakage inventory.** (i) Overlapping labels — a 30-minute triple-barrier label held across adjacent bars overlaps its neighbours' label windows; (ii) one-bar leak — using the current bar's close/high/low to decide inside that bar; (iii) session boundaries — the session window must be defined in one clock convention and audited for timezone/DST drift; (iv) feature windows spanning the close — daily indicators used intraday must be **t−1** inputs; (v) overnight legs — separate close→open and open→close and use only what was observable at signal time [20]. A practical audit: truncate the dataset at a random point and verify the signal at that point is unchanged; if it changes, future data leaked backwards [20].

21. **Microstructure noise corrupts intraday inference itself.** Bid–ask bounce induces negative lag-1 autocorrelation in transaction-price returns even under an efficient random walk; realized variance becomes biased as sampling frequency rises, so intermediate sampling or noise-robust estimators are required [21].

### C. How much data is enough; the SR·√T trap; effective sample size

22. **The t = SR·√years trap.** With annualized SR and years Y, t ≈ SR·√Y, so **Y ≳ (1.96/SR)²** for two-sided 95%: SR 1.0 → ~3.8 years; SR 0.5 → ~15.4 years [17]. Under MinTRL/power-aware treatments with monthly data the requirements rise to roughly 7 years (SR 1.0) and 26 years (SR 0.5) [5][17]. The trap for intraday: high trade counts make t = s·√N_trades look large while the *annualized* edge is tiny, and overlapping/autocorrelated trade returns make the effective N far smaller than the raw count.

23. **Effective sample size.** For a stationary series, N_eff = N/(1 + 2Σρ_k) [22]; the AR(1) shortcut is N_eff ≈ N(1−ρ)/(1+ρ) [22]. Positive autocorrelation inflates a naive Sharpe because the denominator uses return volatility while the precision of the mean is degraded by dependence [22].

24. **Lo's autocorrelation-corrected annualization.** SR_Lo = SR_p · q/√(q + 2Σ_{k=1}^{q−1}(q−k)ρ_k); this reduces to the textbook √q rule exactly when all ρ_k = 0 [23]. Lo's correction is about correct annualization under serial dependence, not a small-sample bias tweak [23].

25. **Newey–West / HAC.** HAC standard errors are the correct basis for t-tests on serially correlated returns; the widely used Bartlett bandwidth rule is **L = ⌊4(n/100)^(2/9)⌋** (a rule of thumb, *not* Andrews' 1991 optimal plug-in selector) [24][22]. HAC does not "fix" the Sharpe itself — it fixes inference around the mean/coefficients [22].

26. **Worked data-requirement numbers (our construction, formulas [7][22][23]).** With 500 trades/year and per-trade Sharpe s, t = s·√500 = 22.36·s: s=0.05 → t=1.12; s=0.09 → t=2.01; s=0.135 → t=3.02. So in one year, only a per-trade Sharpe ≥ ~0.09 reaches two-sided 95%, ≥ ~0.13 reaches the HLZ hurdle. If daily returns carry ρ₁=0.2, ρ₂=0.1, ρ₃=0.05 (Σρ=0.35), then over 504 sessions N_eff = 504/1.70 ≈ 296, and a raw t of 2.0 deflates to ≈ 2.0·√(296/504) = 1.53.

### D. Comparing two sleeves fairly

27. **Comparing aggregate Sharpe alone is wrong.** The Strategy Approval Theorem: for S strategies with equal-volatility weighting, the benchmark Sharpe is SR_bench = SR̄·√(S/(1+(S−1)ρ̄)) and its asymptotic maximum is **SR̄/√ρ̄** [4, Eqs. 6, 11]. Their own example: S=16, average SR 0.75, average ρ 0.2 → benchmark SR **1.50**, i.e. 0.75·√(16/4)=1.5. Adding a strategy with *below-average or even negative* SR can raise the portfolio Sharpe if its average correlation is low enough — in their τ=... example the indifference correlation to accept a negative-Sharpe candidate was **−0.439** [4]. Therefore the correct sleeve question is not "which Sharpe is higher?" but "does adding sleeve B raise SR_house = SR̄/√ρ̄, and at what weight?" [4].

28. **Paired tests for two strategies.** (i) **Ledoit–Wolf** studentized time-series bootstrap CI for Δ = ζ₁−ζ₂; reject equality if 0 ∉ CI; use stationary/circular block bootstrap when returns are serially correlated or heteroskedastic [15]. (ii) **Diebold–Mariano**: DM = d̄/√(âvar(d̄)) with d_t the loss differential and a HAC long-run variance; classic DM is for *forecast* loss and is inappropriate for nested models (use Clark–West) and is not natively a Sharpe test [13][15]. (iii) **McNemar** for paired binary win/loss on the same signals: χ² = (b−c)²/(b+c) over discordant pairs, exact binomial when counts are small [16]. (iv) **Paired/stationary bootstrap** for CIs on continuous metrics (expectancy, mean return, Sharpe); MSE-optimal expected block length for variance estimation is O(n^{1/3}) [14][16].

29. **Tail dependence beats average correlation for co-drawdown risk.** Correlation is a full-sample average; diversification weakens in stress. The right statistics are **lower-tail dependence** λ_L (P(one sleeve extreme-negative | other extreme-negative)) and **co-drawdown coincidence**, clustered by tail behaviour rather than average ρ [25].

### E. Portfolio-level inference (weights, 1/N, estimation-error tax)

30. **1/N is hard to beat.** DeMiguel, Garlappi & Uppal compare **14 optimization models across 7 datasets** and find none consistently beats 1/N on Sharpe, certainty-equivalent return or turnover; calibrated to US equities, the sample-based mean-variance strategy needs **~3,000 months (25 assets)** and **~6,000 months (50 assets)** to outperform 1/N [26]. The stated mechanism: "the gain from optimal diversification is more than offset by estimation error" [26].

31. **Weight-estimation error and the 70/30 question.** With equal sleeve volatilities and ρ=0.15, a hard 70/30 split of SRs 0.9 and 1.6 gives SR_p = (0.7·0.9+0.3·1.6)/√(0.49+0.09+2·0.21·0.15) = 1.11/0.802 = **1.384**, whereas equal-vol gives 1.25/√(0.575) = **1.649** [4, Eqs. 2–6]. Any fixed split is therefore a bet that the better sleeve stays better; the indifference-curve weights (re-derived under shrinkage) dominate a hardcoded split [4][26].

### F. Cost-inclusive validation and the backtest→live gap

32. **Model costs as implementation shortfall.** Perold's benchmark is the decision (arrival) price, not the execution price or a later close; shortfall = paper-portfolio return − actual-portfolio return, decomposed into execution cost on filled shares and opportunity cost on unfilled quantity [27].

33. **Realistic cost levels (US cash equities, 2025–2026).** Liquid large-cap effective spreads ≈ **0.7–1 bp** (arrival quoted ≈2 bps; marketable retail ≈0.74 bps); round-trip all-in **5–10 bps** large caps, **10–20 bps** mid-caps, **20–50+ bps** small caps; measured retail round-trip costs have been reported over a wide 7–46 bps excluding commissions [20, cost survey; REPORTED, vendor/microstructure study — treat as a lead]. Regulatory: **SEC Section 31 = $20.60 per $1,000,000 effective 2026-04-04** (was $0.00 from 2025-05-14 after the FY2025 appropriation); **FINRA TAF = $0.000195/share with a $9.79 cap (2026)**, $0.000166/$8.30 (2025) [28].

34. **Borrow costs.** General collateral ≈ **0.05–0.50%/yr (often ~0.17–0.25%)**; hard-to-borrow ≈ **5–50%+/yr**, exceeding 100% in squeezes; daily cost = position × rate/365 (some brokers use 360) [29]. Short sleeves must model borrow or they overstate net Sharpe.

35. **Market impact / capacity.** Square-root law I(Q) ≈ Y·σ·√(Q/V) with Y of order unity; log-log fits give exponents ≈0.31 but the square-root form is preferred to avoid overfitting and to match theory [30]. Capacity is often defined as the AUM where market impact reaches **50 bps/year** [30, REPORTED/secondary]. Worked: Q/V = 1% of ADV with σ_daily = 2% → impact ≈ 0.02·0.10 = **20 bps one-way** [30].

36. **The paper→live gap, quantified.** Alpaca's own documentation states paper trading does **not** account for market impact, information leakage, price slippage due to latency, order queue position, price improvement, regulatory fees, or dividends; the paper account also does **not** simulate borrow fees ("coming soon") and does **not** check order quantity against NBBO size — you can be filled for far more than the real available liquidity [31]. Documented backtest→live degradation for intraday/high-turnover strategies clusters around **20–60%** of expected profit (vendor/SEO-sourced; treat as a lead, not a fact) [20].

37. **Turnover destroys net alpha.** A composite of short-term signals with ~6%/yr gross alpha fell to **≈ −2%/yr net** at 25 bps per round trip, and one composite lost ~66% of alpha after costs; one high-turnover intraday futures example had ~7,000% daily turnover and a net Sharpe of −21 [32, REPORTED/aggregated]. Anomaly strategies can survive: Frazzini–Israel–Moskowitz report net expected returns of **2.92% (size) to 5.39% (four-strategy combo)** and **5.37% (UMD)** net of costs [30].

38. **The cost arithmetic our sleeves must face (our construction).** Cost drag = N_trips × c bps = N_trips·c/100 % of capital per year. At 1,000 trips/year and 10 bps round trip, drag = **100%/yr**; at 4 trips/day (1,008/yr) an 8 bps/trip gross edge nets **−2 bps/trip ≈ −20%/yr**. Implication: an intraday sleeve needs >10 bps gross edge per trip to survive 10 bps round-trip costs, before impact and borrow.

39. **Biases that shrink any reported track record.** Hedge-fund database survivorship bias ≈ **2–4%/yr** and backfill bias ≈ **1–1.5%/yr** (combined ~3–4.5%/yr) [33, REPORTED/academic summaries]. McLean & Pontiff: average predictor return is **26% lower out-of-sample** and **58% lower post-publication** (32 pp incremental, attributed to publication-informed trading) [34].

### G. What a credible 2026 real-money intraday track record looks like

40. **Statistical floor, not folklore.** Practitioner sources converge on "30–50 trades minimum, 50–100 preferred, 2–3 months" before live capital [20, SEO/folklore — flagged]. That floor is *far* below what the statistics above require: with per-trade Sharpe ~0.09, 50–100 trades gives t ≈ 0.6–0.9. A defensible promotion gate must be DSR/PBO/MinBTL-based, not trade-count folklore [1][2][7].

41. **Reporting standard for an external-quality record.** GIPS requires an initial composite presentation of **at least 5 years** of compliant performance (or since inception if younger), then +1 year annually until 10 years [35]. A 2026 intraday record shorter than 12 months cannot be presented credibly under any recognised standard; 5 years is the external bar.

42. **Regulatory context changed in 2026 (directly relevant to an intraday sleeve).** The legacy FINRA pattern-day-trader rule (4+ day trades in 5 business days, $25,000 minimum equity) has been **replaced by intraday margin standards effective 2026-06-04**, with broker phase-in to **2027-10-20**; the $25,000 minimum is eliminated [36][37]. Margin accounts still generally require $2,000 minimum equity [37]. Broker implementation during the transition is the binding unknown.

43. **A real, documented intraday effect to benchmark against.** Gao, Han, Li & Zhou (2018) find the first half-hour return positively predicts the last half-hour return on the S&P 500 ETF, 1993–2013, stronger on high-volatility, high-volume, recession and macro-news days, and present in ten other actively traded ETFs [38]. This is PROVEN *in-sample predictability*; its survival net of realistic intraday spreads, queue and impact is NOT established by the paper — use it as an upper bound on what an intraday edge looks like, not a strategy.

---

## Numbers to design against

| Parameter | Screening gate | Promotion gate (live capital) | Source / formula |
|---|---|---|---|
| Declared independent trials N | mandatory, append-only registry | mandatory | DSR Eq. 1 needs N; without N reject [1] |
| Cross-trial SR dispersion sd(SR_trials) | estimate from all logged trials | estimate; use N_eff from avg correlation | [1, Eq. 1 & App. 3] |
| Minimum trades per setup | ≥ 300 closed trades | ≥ 1,000 closed trades | t = s·√N_trades; need s ≥ 0.09 for t=2 at 500 trades/yr (our calc) |
| Per-trade Sharpe s to reach t=2 in 1 yr (500 trades) | s ≥ 0.09 | s ≥ 0.13 (t=3, HLZ hurdle) | [7][6] |
| Per-trade Sharpe s to reach t=2 in 6 months (250 trades) | s ≥ 0.126 | — | our calc |
| Minimum OOS months | ≥ 12 months (250 sessions) | ≥ 24 months, and ≥ MinBTL | [3][17] |
| MinBTL (years) | — | ≥ 2·ln(N)/SR_IS² (upper bound) | [3][18] |
| t-statistic threshold | ≥ 2.0 two-sided w/ HAC | ≥ **3.0** (HLZ); use 3.78/3.9 if ~300+ tests | [6][18] |
| DSR | ≥ 0.90 | ≥ **0.95** | [1] |
| PBO (CSCV, S=16) | ≤ 0.10 | ≤ **0.05** | [2] |
| MinTRL | compute; report | SR̂ > SR* with 95% conf | [5] |
| Haircut Sharpe (BHY) | report raw + haircut; reject if haircut > 50% (SR<0.5) | accept if haircut ≤ 25% (SR>1.0) | [7] |
| Effective sample size N_eff | ≥ 250 | ≥ 500 | N_eff = N/(1+2Σρ_k) [22] |
| Newey–West lags L | ⌊4(n/100)^(2/9)⌋ (n=504 → L=5; n=100k → L=18) | max(L, label_horizon+1) | [24] |
| ΔSR test | paired stationary bootstrap, block ≈ ⌈n^{1/3}⌉; 95% CI excludes 0 | same, plus DM \|stat\| > 1.96 on daily return differential | [15][13][14] |
| McNemar (paired win/loss) | ≥ 25 discordant pairs; χ² > 3.84 | same | [16] |
| Daily-return correlation ρ | target ≤ 0.30; warn 0.30–0.50; reject ≥ 0.70 | ≤ 0.30 | [4][25] |
| Downside (below-zero) correlation | ≤ 0.40 | ≤ 0.30 | [25] |
| Lower-tail dependence λ_L | ≤ 0.30 | ≤ 0.20 | [25] |
| Co-drawdown coincidence (Jaccard of drawdown intervals) | ≤ 0.25 | ≤ 0.15 | [25] |
| Stress-window correlation (worst-decile vol days) | ≤ 0.60 | ≤ 0.45 | [25] |
| Round-trip cost assumption | ≥ 10 bps liquid large caps | ≥ 20 bps conservative + borrow | [20][28][29] |
| Impact model | Y·σ·√(Q/V), Y≈1; cap ≤ 1% ADV | capacity at 50 bps annual impact | [30] |
| Borrow | GC 0.05–0.50%/yr | HTB 5–50%+/yr modelled explicitly | [29] |
| Net-vs-gross | report both | promote only on net-of-cost DSR ≥ 0.95 | [7][32] |
| Promotion ladder | Alpaca paper ≥ 3 months + ≥ 250 trades (knowing impact/queue/fees/borrow/dividends unmodelled) | live micro ≤ 5% capital ≥ 3 months | [31] |

### Exact metric list for the sleeve-vs-sleeve comparison (the owner's ask)

**Same universe, same start capital, same session calendar, daily returns stamped at session close, all metrics NET of modelled costs.**

*Per-sleeve performance:* CAGR; annualized vol; annualized Sharpe (net); Sortino; Calmar; max drawdown; drawdown duration; time-to-recovery; % winning days; average win/avg loss; profit factor; skewness; kurtosis; daily VaR95; daily CVaR95; turnover; average holding period; trades/day; capital utilization.

*Per-sleeve inference:* number of trades; number of independent trials run to find the rule; N_eff; HAC t on mean daily net return (NW lag L); DSR; PBO via CPCV; MinTRL; bootstrap 95% CI for Sharpe; BHY haircut Sharpe; MinBTL.

*Pairwise:* ΔSR with 95% stationary-bootstrap CI (paired blocks); Diebold–Mariano statistic on the daily return/loss differential; McNemar χ² on paired trade outcomes; Pearson ρ of daily returns; rolling 60-day ρ (min/median/max); downside correlation; lower-tail dependence λ_L; co-drawdown Jaccard; stress-day (worst-decile vol) correlation.

*House-level:* SR_house with each sleeve, SR_house with neither, incremental Sharpe = SR_house∪sleeve − SR_house_alone; contribution to house drawdown; correlation of each sleeve to the house risk-gate state (volatility regime label).

*Costs/capacity:* gross vs net Sharpe; bps per trade; slippage vs arrival price; participation rate (% ADV); implied capacity at 50 bps annual impact.

---

## Contested or unproven

- **Var(SR_trials) and N are unobservable in practice.** Bailey et al. admit the average-correlation route to N_eff is itself overfit when M > T and the correlation matrix is ill-conditioned; they recommend information-theoretic (entropy/total-correlation) alternatives but provide no hard selector [1, App. 3]. DSR is therefore only as honest as the trial registry feeding it.
- **The DSR non-normality correction is a plug-in with known weak spots.** It uses sample skew/kurtosis of the *selected* strategy's returns; for fat-tailed intraday P&L (kurtosis 6–10) the correction is large and the sampling error of γ̂₃, γ̂₄ at 175–500 observations is high [1].
- **PBO ≤ 0.05 is a convention, not a theorem.** Bailey et al. call it "a customary approach" [2, §3.1]. The 0.10 alternative is defensible for a two-sleeve screening exercise.
- **The HLZ t > 3.0 hurdle is not a law and its variants (2.78 / 3.78 / 3.9) are method-dependent** [6][8][18]. It is calibrated on monthly cross-sectional equity factors, not intraday P&L — the transfer is an omission-by-analogy.
- **Harvey–Liu haircuts inherit the HLZ factor-return distribution model** (fitted on 300+ monthly equity factors) and assume a correlation level across trials; the authors themselves list five judgement calls (significance level, method, number of tests, etc.) [7]. Applying it to intraday is an extrapolation.
- **Diebold–Mariano is a forecast-loss test, not a Sharpe test.** Using it on a return differential is a common but non-canonical extension, and it is invalid for nested models (Clark–West required) [13][15].
- **Optimal block length for the bootstrap is genuinely contested.** Politis–Romano n^{1/3} is MSE-optimal for variance estimation only; Politis–White 2004 provides a plug-in selector, and coverage is sensitive to block choice [14].
- **"30–50 trades, 2–3 months" promotion folklore is unsourced.** The trade counts trace to retail-day-trading listicles, not to power analysis; the statistical requirement is far higher [20, flagged SEO].
- **Backtest→live "20–60% degradation" is vendor/SEO-sourced**, with the credible primary statement being narrower: Alpaca *documents which frictions paper trading omits*, not *how much* live performance falls [31].
- **Tail-dependence and co-drawdown estimators are extremely noisy at n≈175.** Any λ_L or Jaccard cutoff applied to a single 8.5-month window is descriptive, not inferential [25].
- **The 1/N result is about *asset* portfolios; applying it to two sleeves is analogy.** The 3,000/6,000-month figures are for 25/50 assets, not two [26].
- **PDT replacement is real but mid-transition.** Effective 2026-06-04 with phase-in to 2027-10-20, so broker-specific intraday-margin behaviour in 2026 is not yet stable [36][37].

---

## Implications for our system

- **Add a `validation/` component in TradingExecution emitting a signed `validation_card.json` next to `run_card.json`** with mandatory fields: `n_trials`, `sd_sr_trials`, `sr_period`, `n_obs`, `skew`, `kurt`, `dsr`, `pbo`, `minbtl_years`, `mintrl_obs`, `haircut_sr_bhy`, `n_eff`, `nw_lags`, `sr_gross`, `sr_net`. Fail closed if `n_trials` is missing — an undeclared trial count makes the artifact inadmissible [1][2][7].
- **Replace any `t = SR·√years` check with a HAC t and an N_eff-adjusted n.** Enforce NW lags `L = max(⌊4(n/100)^(2/9)⌋, label_horizon_obs + 1)` and reject a promotion when N_eff < 500 [22][23][24].
- **Implement purged + embargoed walk-forward as the only CV path for intraday, and CPCV (e.g. N=6, k=2 → 15 folds) to produce a distribution of OOS paths for PBO.** Embargo must be ≥ the label horizon + 1 bar; all scalers/feature selection fitted inside the training fold only [17][19].
- **Define the sleeve-vs-sleeve comparison as a contract, not a report:** aligned daily net-return series at session close; ΔSR with a paired stationary-bootstrap 95% CI (block ≈ ⌈n^{1/3}⌉); DM statistic on the daily differential; McNemar on paired trades; and the full pairwise panel (ρ, rolling ρ, downside ρ, λ_L, co-drawdown Jaccard, stress-day ρ). Publish the CI, never two Sharpe point estimates [13][14][15][16][25].
- **Make the house risk gate emit two distinct, machine-readable fields** so option (d) of the owner's spec is enforced structurally: `opportunity_score` (valuation; low ⇒ "no BUY for valuation") vs `trade_permission` + `binding_gate` (risk; score high but BLOCKED ⇒ names the binding constraint, e.g. `cvar_95`, `drawdown_budget`, `corr_stress`, `vol_regime`, `knife_guard`). The validation layer must log both and must never conflate them [1][4].
- **Derive the sleeve capital split from the Sharpe/correlation indifference curve, not a hardcoded 70/30.** Compute SR_house = SR̄·√(S/(1+(S−1)ρ̄)) for {A}, {B} and {A,B}; accept sleeve B only if it raises SR_house; re-derive weights monthly under a shrinkage rule and cap cross-sleeve capital borrowing behind the house gate [4][26].
- **Mandatory cost model in every sleeve backtest before any validation statistic is computed:** ≥10 bps round-trip for liquid large caps, 20 bps conservative, explicit borrow for shorts, participation cap ≤1% ADV, and a square-root impact term. Promote only on net-of-cost DSR ≥ 0.95 and a net-of-cost MinBTL ≤ the available sample [1][29][30][32].
- **Add an explicit kill-switch on cost/edge ratio.** Block any configuration whose modelled round-trip cost exceeds ~40% of expected per-trip gross edge, and refuse capacity claims above the AUM where sqrt-law impact reaches 50 bps/year [30][32].
- **Define the promotion ladder in code, gated on both calendar and statistics:** (1) paper (Alpaca paper ≥3 months, ≥250 trades) with an explicit acknowledgment that impact, queue, latency slippage, price improvement, regulatory fees, dividends and borrow are unmodelled; (2) live-micro ≤5% of allocated sleeve capital ≥3 months; (3) scale only when DSR ≥0.95, PBO ≤0.05, N_eff ≥500, and MinTRL/MinBTL are satisfied net of costs. Keep a separate GIPS-style 5-year presentation standard for any external reporting [1][2][31][35].
- **Log, per sleeve, the realized implementation shortfall against the arrival (decision) price** so the paper→live gap is measured rather than assumed, and feed it back as the cost parameter in the next validation cycle [27].

---

## Worked examples

### W1. Deflated Sharpe Ratio, realistic intraday inputs (our construction, following [1, Eqs. 1–2])

Inputs for the Intraday Algo sleeve: N = 500 configurations tried (entry window × exit rule × stop × session filter); sd(SR_trials) = 0.50 (annualized, under H0); observed best SR = 2.00 annualized; T = 504 daily observations (2 years); γ₃ = −0.80; γ₄ = 7.0. Daily conversion factor √252 = 15.874.

1. maxZ = 0.4228·Φ⁻¹(0.998) + 0.5772·Φ⁻¹(1 − 1/(500·2.71828)) = 0.4228·2.87816 + 0.5772·3.18983 = 1.21689 + 1.84117 = **3.05806**.
2. SR₀(annualized) = 0.50 × 3.05806 = **1.5290**; SR₀(daily) = 1.5290/15.874 = **0.09632**.
3. SR̂(daily) = 2.00/15.874 = **0.12600**.
4. Denominator = √(1 − (−0.80)(0.12600) + ((7.0−1)/4)(0.12600)²) = √(1 + 0.10080 + 0.02381) = **1.06051**.
5. z = (0.12600 − 0.09632)·√503 / 1.06051 = 0.02968 × 22.4266 / 1.06051 = **0.6276**.
6. **DSR = Φ(0.6276) = 0.735** → FAIL (the 0.95 bar). Interpretation: with 500 trials and 2 years, a Sharpe-2.0 intraday backtest has only ~73% probability that the true SR exceeds zero.

Sensitivity (same γ₃, γ₄, T; SR₀ scales with N; z = (SR_d − SR₀_d)·√503/denom):

| N | SR₀ (ann.) | DSR @ SR 1.5 | DSR @ SR 2.0 | DSR @ SR 2.5 |
|---|---|---|---|---|
| 10 | 0.788 | 0.833 | 0.947 | 0.988 |
| 50 | 1.139 | 0.688 | 0.875 | 0.963 |
| 100 | 1.266 | 0.625 | 0.836 | 0.947 |
| 500 | 1.529 | 0.484 | **0.735** | 0.898 |

To clear DSR = 0.95 at N = 500 and T = 504: z must reach 1.645, so SR_d = 0.09632 + 1.645·1.06051/22.4266 = **0.17408**, i.e. **SR_annualized ≈ 2.76**. A Sharpe of 2.0 that feels impressive is *not* promotable after 500 trials.

### W2. Paired bootstrap: Value-Dip Swing vs Intraday Algo, 2026-01-02 → 2026-09-11

Inputs: n = 175 sessions; Sleeve A ann. SR 0.90, daily vol 0.80% → 4.54 bps/day; Sleeve B ann. SR 1.60, daily vol 0.60% → 6.05 bps/day; ρ_daily = 0.15.

1. ΔSR_daily = 0.90/15.874 − 1.60/15.874 = 0.05670 − 0.10079 = **−0.04409**.
2. Asymptotic SE(SR_i) ≈ √((1 + SR_i²/2)/n): A → √(1.001607/175) = **0.075652**; B → √(1.005079/175) = **0.075787**.
3. Cov ≈ ρ(1 + ρ·SR_A·SR_B/2)/n = 0.15·(1.000043)/175 = **0.00085718**.
4. Var(Δ) = 0.0057232 + 0.0057433 − 2(0.00085718) = 0.0097521 → **SE(Δ) = 0.098753**.
5. **t = −0.04409/0.098753 = −0.447** (p ≈ 0.66). 95% CI = −0.0441 ± 1.96·0.0988 = **[−0.238, +0.150]** — includes 0.
6. Required n for t = 2: n = 175·(2/0.447)² ≈ **3,510 sessions ≈ 13.9 years**.

General requirement (iid approximation, n ≈ 4·2(1−ρ)(1+SR̄²/2)/ΔSR_d²):

| ΔSR (annualized) | ΔSR (daily) | Sessions for t=2 (ρ=0.15) | Years |
|---|---|---|---|
| 0.70 | 0.0441 | 3,506 | 13.9 |
| 1.50 | 0.0945 | 764 | 3.0 |
| 3.00 | 0.1890 | 191 | 0.76 |

**Conclusion for the owner's apples-to-apples ask:** the requested 2026-01-02 → 2026-09-11 window (~175 sessions) cannot support a sleeve-vs-sleeve ranking. The comparison must be run on the longest available common history and reported as a ΔSR confidence interval, with the 2026 window treated as a monitoring sub-period only.

### W3. McNemar and cost checks (supporting)

- **McNemar:** 300 paired trades, b = 60 (A wins/B loses), c = 85 (B wins/A loses): χ² = (60−85)²/145 = 625/145 = **4.31 > 3.84** → reject equal win rates (with ≥25 discordant pairs the normal approximation is adequate).
- **Cost drag:** 4 round trips/day × 252 = 1,008 trips/yr. At 10 bps round trip, drag = 1,008 × 10 bps = **100.8% of capital per year**; an 8 bps/trip gross edge nets −2 bps/trip ≈ **−20%/yr**. An intraday sleeve therefore needs >10 bps gross edge per trip before impact and borrow.
- **Impact:** Q/V = 1% of ADV, σ_daily = 2% → impact ≈ 0.02·√0.01 = 0.02·0.10 = **20 bps one-way** under I = Y·σ·√(Q/V) with Y≈1.
- **HAC lags:** n = 504 → L = ⌊4(5.04)^{2/9}⌋ = ⌊4·1.4325⌋ = **5**; n = 100,000 (1-min bars over 2 years) → L = ⌊4·1000^{2/9}⌋ = ⌊4·4.6416⌋ = **18**.

---

## Sources

1. Bailey, D. H., & López de Prado, M. — *The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality* — Journal of Portfolio Management 40(5):94–107, 2014 — https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf (2014)
2. Bailey, D. H., Borwein, J. M., López de Prado, M., & Zhu, Q. J. — *The Probability of Backtest Overfitting* — Journal of Computational Finance (working paper, Feb 2015) — https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf (2015)
3. Bailey, D. H., Borwein, J. M., López de Prado, M., & Zhu, Q. J. — *Pseudo-Mathematics and Financial Charlatanism: The Effects of Backtest Overfitting on Out-of-Sample Performance* — Notices of the AMS 61(5):458–471, May 2014 — https://www.ams.org/notices/201405/rnoti-p458.pdf (2014)
4. Bailey, D. H., López de Prado, M., & del Pozo, E. — *The Strategy Approval Decision: A Sharpe Ratio Indifference Curve Approach* — Algorithmic Finance 2(1) — https://www.davidhbailey.com/dhbpapers/sharpe-ratio.pdf (2012)
5. Bailey, D. H., & López de Prado, M. — *The Sharpe Ratio Efficient Frontier* (Probabilistic Sharpe Ratio, Minimum Track Record Length) — Journal of Risk 15(2), Winter 2012/13 — SSRN 1821643 (2012)
6. Harvey, C. R., Liu, Y., & Zhu, H. — *…and the Cross-Section of Expected Returns* — Review of Financial Studies 29(1):5–68 — https://people.duke.edu/~charvey/Research/Published_Papers/P118_and_the_cross.pdf (2016)
7. Harvey, C. R., & Liu, Y. — *Backtesting* — Journal of Portfolio Management 42(1):13–28, Fall 2015 — https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.pdf (2015)
8. Harvey, C. R., & Liu, Y. — *Lucky Factors* — Journal of Financial Economics (working paper 2015) — SSRN 2528780 (2015)
9. White, H. — *A Reality Check for Data Snooping* — Econometrica 68(5):1097–1126 — DOI 10.1111/1468-0262.00152 (2000)
10. Sullivan, R., Timmermann, A., & White, H. — *Data-Snooping, Technical Trading Rule Performance, and the Bootstrap* — Journal of Finance 54(5):1647–1691 — DOI 10.1111/0022-1082.00163 (1999)
11. Hansen, P. R. — *A Test for Superior Predictive Ability* — Journal of Business & Economic Statistics 23(4):365–380 (2005)
12. Romano, J. P., & Wolf, M. — *Stepwise Multiple Testing as Formalized Data Snooping* — Econometrica 73(4):1237–1282 (2005)
13. Diebold, F. X., & Mariano, R. S. — *Comparing Predictive Accuracy* — Journal of Business & Economic Statistics 13(3):253–263 (1995)
14. Politis, D. N., & Romano, J. P. — *The Stationary Bootstrap* — Journal of the American Statistical Association 89(428):1303–1313 (1994)
15. Ledoit, O., & Wolf, M. — *Robust Performance Hypothesis Testing with the Sharpe Ratio* — Journal of Empirical Finance 15(5):850–859 (2008)
16. McNemar, Q. — *Note on the sampling error of the difference between correlated proportions or percentages* — Psychometrika 12(2):153–157 (1947); practitioner paired-bootstrap guidance (secondary)
17. López de Prado, M. — *Advances in Financial Machine Learning* (Ch. 7 purged k-fold/embargo; Ch. 12 CPCV), Wiley — and *Combinatorial Purged Cross-Validation*, SSRN 3257497 (2018)
18. Harvey, C. R. — *Evaluating Trading Strategies* / HLZ cutoff discussion — Duke Fuqua working papers and backtesting code page — http://faculty.fuqua.duke.edu/~charvey/backtesting (2015)
19. Practitioner/academic walk-forward guidance, anchored vs rolling and re-optimization leakage (aggregated secondary; treat as lead) (2024–2025)
20. Intraday backtest leakage checklist and cost surveys (aggregated secondary; SEO/vendor — leads only) (2024–2025)
21. Andersen, T. G., Bollerslev, T., Diebold, F. X., & Labys, P. — *The Distribution of Realized Exchange Rate Volatility* / microstructure-noise literature (bid–ask bounce, negative lag-1 autocorrelation) — JASA 96(453):42–55 (2001)
22. Newey, W. K., & West, K. D. — *A Simple, Positive Semi-Definite, Heteroskedasticity and Autocorrelation Consistent Covariance Matrix* — Econometrica 55(3):703–708 (1987); effective-sample-size HAC guidance (secondary) (1987/2024)
23. Lo, A. W. — *The Statistics of Sharpe Ratios* — Financial Analysts Journal 58(4):36–52 (2002)
24. Andrews, D. W. K. — *Heteroskedasticity and Autocorrelation Consistent Covariance Matrix Estimation* — Econometrica 59(3):817–858 (1991)
25. Tail-dependence and stress-correlation literature (Longin & Solnik tail dependence; "correlation breakdown" surveys — secondary/aggregated) (2001–2025)
26. DeMiguel, V., Garlappi, L., & Uppal, R. — *Optimal Versus Naive Diversification: How Inefficient is the 1/N Portfolio Strategy?* — Review of Financial Studies 22(5):1915–1953 (2009)
27. Perold, A. F. — *The Implementation Shortfall: Paper vs. Reality* — Journal of Portfolio Management 14(3):4–9 (1988)
28. SEC — *Section 31 fee rate* ($20.60 per $1,000,000, effective 2026-04-04; $0.00 from 2025-05-14) — https://www.sec.gov ; FINRA — *Trading Activity Fee schedule* ($0.000195/share, $9.79 cap, 2026) — https://www.finra.org (2026)
29. Short-interest/borrow-cost reference material — general collateral ≈0.05–0.50%/yr, hard-to-borrow ≈5–50%+/yr (secondary/aggregated) (2025)
30. Frazzini, A., Israel, R., & Moskowitz, T. J. — *Trading Costs* — SSRN 3229719 (2018); capacity definition per Research Affiliates commentary (secondary)
31. Alpaca — *Paper Trading* documentation (omits market impact, information leakage, latency slippage, queue position, price improvement, regulatory fees, dividends; borrow fees not simulated; no NBBO quantity check) — https://docs.alpaca.markets/us/docs/paper-trading (updated 2026-07-07)
32. Intraday/turnover transaction-cost studies (gross ≈6%/yr → net ≈ −2%/yr at 25 bps per round trip; ~66% alpha loss; −21 net Sharpe on 7,000% daily turnover) (aggregated secondary; leads) (2023–2025)
33. Hedge-fund database bias estimates: Liang (2000) survivorship >2%/yr; Fung & Hsieh (2000) survivorship ≈3%/yr, backfill ≈1.4%/yr (2000)
34. McLean, R. D., & Pontiff, J. — *Does Academic Research Destroy Stock Return Predictability?* — Journal of Finance 71(1):5–32 (2016)
35. CFA Institute — *GIPS Standards* (minimum 5-year initial track record, +1 yr to 10 yrs) — https://www.cfainstitute.org (2020)
36. FINRA — *Regulatory Notice 26-10* (intraday margin standards replace PDT framework; effective 2026-06-04; phase-in to 2027-10-20) — https://www.finra.org (2026)
37. FINRA — pattern-day-trader rule background ($25,000 minimum; 4 day trades / 5 business days; $2,000 margin-account minimum) (secondary summaries) (2025–2026)
38. Gao, L., Han, Y., Li, S. Z., & Zhou, G. — *Market Intraday Momentum* — Journal of Financial Economics 129(2):394–414 (2018)

---

*Drafting note: items tagged REPORTED / secondary / lead are vendor, SEO or aggregator sources used only to locate primary material or to bracket a number; every load-bearing threshold above traces to a primary source ([1]–[27], [30], [31], [34]–[38]). Worked examples W1–W3 are our own constructions from the cited formulas, not quotations from the sources.*
