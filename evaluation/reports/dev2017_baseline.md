# Detection evaluation - dev2017

**2017 development test period (fit period 2017-04-26 to 2017-08-27; Black Friday week excluded)**

git `57209970e53cf3707c1d7dc5e9eae3a8c27460a9-dirty` · data `8333919867b2` · detector `9d38ab845770` · scenarios `948accb0ff7b` · period 2017-08-28 to 2017-12-31 · last reliable day 2018-08-24

## Headline

| | Lenient (NFR-021 comparator) | Strict |
|---|---|---|
| Recall, faults with measured magnitude >= 20% | **43.8% (14/32; 95% CI 28%-61%)** | 25.0% (8/32; 95% CI 13%-42%) |
| Recall, all scenarios | 29.6% (16/54; 95% CI 19%-43%) | 14.8% (8/54; 95% CI 8%-27%) |

- Lenient: fault-caused alert on an affected KPI, any monitored series, from the first affected day to 7 days after the fault ends (the Phase 3 definition).
- Strict: fault-caused alert on an affected KPI, on the targeted segment's own series (total for faults not aimed at a monitored dimension), within 3 days of the first affected day.
- **NFR-021 is judged on the lenient figure**: the baseline and the README were measured with the lenient definition; a like-for-like comparison needs the same one. Strict is shown beside it as the more demanding reading.
- NFR-021 recall target 80%: lenient **NOT met**, strict NOT met.
- False alarms on clean days: **15.63 per 30 days** (62 alarms on 31 alarm-days in 119 days; target <= 2: NOT met).
- Precision of fault-caused alerts: 84.8%
- Guard alerts: not enabled (change A not implemented)

## False alarms by cause

| cause | alarms |
|---|---|
| dataset_cutoff | 0 |
| calendar_window | 15 |
| known_event | 0 |
| gmv_spike | 14 |
| other | 33 |
| *matches an unlabelled real event (non-exclusive, never reclassified)* | 0 |

## Recall by target size (share of orders: large >= 5%, medium 1-5%, small < 1%)

| size | lenient, all | strict, all | lenient, >= 20% | strict, >= 20% |
|---|---|---|---|---|
| large | 55.6% (10/18; 95% CI 34%-75%) | 16.7% (3/18; 95% CI 6%-39%) | 80.0% (8/10; 95% CI 49%-94%) | 30.0% (3/10; 95% CI 11%-60%) |
| medium | 5.6% (1/18; 95% CI 1%-26%) | 0.0% (0/18; 95% CI 0%-18%) | 8.3% (1/12; 95% CI 1%-35%) | 0.0% (0/12; 95% CI 0%-24%) |
| small | 0.0% (0/12; 95% CI 0%-24%) | 0.0% (0/12; 95% CI 0%-24%) | 0.0% (0/4; 95% CI 0%-49%) | 0.0% (0/4; 95% CI 0%-49%) |
| total | 83.3% (5/6; 95% CI 44%-97%) | 83.3% (5/6; 95% CI 44%-97%) | 83.3% (5/6; 95% CI 44%-97%) | 83.3% (5/6; 95% CI 44%-97%) |

## Recall by measured magnitude

| band | lenient | strict |
|---|---|---|
| <10% | 8.3% (1/12; 95% CI 1%-35%) | 0.0% (0/12; 95% CI 0%-24%) |
| 10-20% | 10.0% (1/10; 95% CI 2%-40%) | 0.0% (0/10; 95% CI 0%-28%) |
| >=20% | 43.8% (14/32; 95% CI 28%-61%) | 25.0% (8/32; 95% CI 13%-42%) |

## Detected within N days of the first affected day

| N | lenient, all | lenient, >= 20% | strict, all | strict, >= 20% |
|---|---|---|---|---|
| 0 | 11.1% (6/54; 95% CI 5%-22%) | 18.8% (6/32; 95% CI 9%-35%) | 11.1% (6/54; 95% CI 5%-22%) | 18.8% (6/32; 95% CI 9%-35%) |
| 1 | 14.8% (8/54; 95% CI 8%-27%) | 25.0% (8/32; 95% CI 13%-42%) | 14.8% (8/54; 95% CI 8%-27%) | 25.0% (8/32; 95% CI 13%-42%) |
| 3 | 14.8% (8/54; 95% CI 8%-27%) | 25.0% (8/32; 95% CI 13%-42%) | 14.8% (8/54; 95% CI 8%-27%) | 25.0% (8/32; 95% CI 13%-42%) |
| 7 | 16.7% (9/54; 95% CI 9%-29%) | 28.1% (9/32; 95% CI 16%-45%) | 14.8% (8/54; 95% CI 8%-27%) | 25.0% (8/32; 95% CI 13%-42%) |
| 14 | 25.9% (14/54; 95% CI 16%-39%) | 37.5% (12/32; 95% CI 23%-55%) | 14.8% (8/54; 95% CI 8%-27%) | 25.0% (8/32; 95% CI 13%-42%) |

## Operating curve (recall at >= 20% vs false alarms)

| FDR level | min severity | clean alarms | false alarms / 30 days | recall >= 20% (lenient) |
|---|---|---|---|---|
| 0.01 | 0.0 | 42 | 10.59 | 34.4% (11/32; 95% CI 20%-52%) |
| 0.01 | 0.5 | 42 | 10.59 | 34.4% (11/32; 95% CI 20%-52%) |
| 0.01 | 1.0 | 39 | 9.83 | 31.2% (10/32; 95% CI 18%-49%) |
| 0.01 | 2.0 | 32 | 8.07 | 25.0% (8/32; 95% CI 13%-42%) |
| 0.02 | 0.0 | 47 | 11.85 | 40.6% (13/32; 95% CI 26%-58%) |
| 0.02 | 0.5 | 47 | 11.85 | 40.6% (13/32; 95% CI 26%-58%) |
| 0.02 | 1.0 | 44 | 11.09 | 37.5% (12/32; 95% CI 23%-55%) |
| 0.02 | 2.0 | 36 | 9.08 | 34.4% (11/32; 95% CI 20%-52%) |
| 0.05 | 0.0 | 62 | 15.63 | 43.8% (14/32; 95% CI 28%-61%) |
| 0.05 | 0.5 | 62 | 15.63 | 43.8% (14/32; 95% CI 28%-61%) |
| 0.05 | 1.0 | 59 | 14.87 | 40.6% (13/32; 95% CI 26%-58%) |
| 0.05 | 2.0 | 45 | 11.34 | 37.5% (12/32; 95% CI 23%-55%) |
| 0.1 | 0.0 | 75 | 18.91 | 50.0% (16/32; 95% CI 34%-66%) |
| 0.1 | 0.5 | 75 | 18.91 | 50.0% (16/32; 95% CI 34%-66%) |
| 0.1 | 1.0 | 71 | 17.90 | 46.9% (15/32; 95% CI 31%-64%) |
| 0.1 | 2.0 | 51 | 12.86 | 34.4% (11/32; 95% CI 20%-52%) |
| 0.2 | 0.0 | 107 | 26.97 | 50.0% (16/32; 95% CI 34%-66%) |
| 0.2 | 0.5 | 106 | 26.72 | 50.0% (16/32; 95% CI 34%-66%) |
| 0.2 | 1.0 | 100 | 25.21 | 50.0% (16/32; 95% CI 34%-66%) |
| 0.2 | 2.0 | 64 | 16.13 | 40.6% (13/32; 95% CI 26%-58%) |

## Scenarios

| id | target | share | band | measured | lenient | delay | strict | strict delay |
|---|---|---|---|---|---|---|---|---|
| S01-segment_level_shift | customer_state=SP | 39.7% | large | 15.3% | no |  | no |  |
| S02-segment_level_shift | category_en=watches_gifts | 5.9% | large | 24.8% | yes | 9 | no |  |
| S03-segment_level_shift | customer_state=RJ | 14.0% | large | 60.7% | yes | 19 | no |  |
| S04-segment_level_shift | category_en=electronics | 2.1% | medium | 23.5% | no |  | no |  |
| S05-segment_level_shift | customer_state=ES | 2.1% | medium | 24.2% | no |  | no |  |
| S06-segment_level_shift | category_en=telephony | 4.4% | medium | 57.7% | yes | 16 | no |  |
| S07-segment_level_shift | customer_state=MS | 0.7% | small | 100.0% | no |  | no |  |
| S08-segment_level_shift | category_en=dvds_blu_ray | 0.0% | small | 0.0% | no |  | no |  |
| S09-segment_level_shift | customer_state=PI | 0.5% | small | 55.6% | no |  | no |  |
| S10-category_price_change | category_en=furniture_decor | 6.3% | large | 14.9% | no |  | no |  |
| S11-category_price_change | category_en=bed_bath_table | 9.8% | large | 30.0% | no |  | no |  |
| S12-category_price_change | category_en=sports_leisure | 8.3% | large | 59.8% | yes | 10 | no |  |
| S13-category_price_change | category_en=telephony | 4.4% | medium | 15.0% | no |  | no |  |
| S14-category_price_change | category_en=electronics | 2.1% | medium | 30.0% | no |  | no |  |
| S15-category_price_change | category_en=baby | 2.8% | medium | 60.0% | no |  | no |  |
| S16-category_price_change | category_en=home_construction | 0.4% | small | 15.0% | no |  | no |  |
| S17-category_price_change | category_en=industry_commerce_and_business | 0.1% | small | 30.0% | no |  | no |  |
| S18-category_price_change | category_en=small_appliances_home_oven_and_coffee | 0.0% | small | 0.0% | no |  | no |  |
| S19-review_collapse | category_en=health_beauty | 7.5% | large | 14.8% | no |  | no |  |
| S20-review_collapse | category_en=watches_gifts | 5.9% | large | 17.2% | no |  | no |  |
| S21-review_collapse | category_en=bed_bath_table | 9.8% | large | 38.8% | yes | 4 | no |  |
| S22-review_collapse | category_en=perfumery | 3.8% | medium | 16.5% | no |  | no |  |
| S23-review_collapse | category_en=telephony | 4.4% | medium | 31.8% | no |  | no |  |
| S24-review_collapse | category_en=stationery | 2.4% | medium | 34.5% | no |  | no |  |
| S25-review_collapse | category_en=home_appliances | 0.3% | small | 80.0% | no |  | no |  |
| S26-review_collapse | category_en=fashion_childrens_clothes | 0.0% | small | 0.0% | no |  | no |  |
| S27-review_collapse | category_en=small_appliances_home_oven_and_coffee | 0.0% | small | 0.0% | no |  | no |  |
| S28-payment_failure | payment_type=credit_card | 75.2% | large | 15.4% | yes | 11 | no |  |
| S29-payment_failure | payment_type=credit_card | 75.2% | large | 29.6% | no |  | no |  |
| S30-payment_failure | payment_type=credit_card | 75.2% | large | 60.2% | yes | 0 | yes | 0 |
| S31-payment_failure | payment_type=boleto | 20.7% | large | 13.5% | no |  | no |  |
| S32-payment_failure | payment_type=boleto | 20.7% | large | 31.7% | yes | 1 | yes | 1 |
| S33-payment_failure | payment_type=boleto | 20.7% | large | 58.7% | yes | 1 | yes | 1 |
| S34-payment_failure | payment_type=voucher | 3.1% | medium | 17.1% | no |  | no |  |
| S35-payment_failure | payment_type=voucher | 3.1% | medium | 26.5% | no |  | no |  |
| S36-payment_failure | payment_type=voucher | 3.1% | medium | 60.0% | no |  | no |  |
| S37-payment_failure | payment_type=debit_card | 1.0% | medium | 40.0% | no |  | no |  |
| S38-payment_failure | payment_type=debit_card | 1.0% | medium | 26.1% | no |  | no |  |
| S39-payment_failure | payment_type=debit_card | 1.0% | medium | 76.0% | no |  | no |  |
| S40-seller_delay | seller_state=MG | 8.3% | large | 2.0% | no |  | no |  |
| S41-seller_delay | seller_state=PR | 6.8% | large | 2.9% | yes | 14 | no |  |
| S42-seller_delay | seller_state=SP | 70.2% | large | 47.4% | yes | 14 | no |  |
| S43-seller_delay | seller_state=RJ | 4.5% | medium | 0.8% | no |  | no |  |
| S44-seller_delay | seller_state=BA | 1.3% | medium | 0.6% | no |  | no |  |
| S45-seller_delay | seller_state=SC | 3.5% | medium | 2.4% | no |  | no |  |
| S46-seller_delay | seller_state=MT | 0.3% | small | 0.0% | no |  | no |  |
| S47-seller_delay | seller_state=SE | 0.0% | small | 0.0% | no |  | no |  |
| S48-seller_delay | seller_state=CE | 0.0% | small | 0.1% | no |  | no |  |
| S49-pipeline_bug | data_quality=fct_orders | 100.0% | total | 29.4% | no |  | no |  |
| S50-pipeline_bug | data_quality=fct_orders | 100.0% | total | 58.5% | yes | 0 | yes | 0 |
| S51-pipeline_bug | data_quality=fct_orders | 100.0% | total | 100.0% | yes | 0 | yes | 0 |
| S52-pipeline_bug | data_quality=fct_orders | 100.0% | total | 25.3% | yes | 0 | yes | 0 |
| S53-pipeline_bug | data_quality=fct_orders | 100.0% | total | 59.4% | yes | 0 | yes | 0 |
| S54-pipeline_bug | data_quality=fct_orders | 100.0% | total | 100.0% | yes | 0 | yes | 0 |

## Clean-run alarms (all counted as false alarms)

| date | kpi | granularity | headline | observed | expected | q | severity | cause |
|---|---|---|---|---|---|---|---|---|
| 2017-08-28 | avg_delivery_days | daily | total=all | 12.67 | 10.18 | 0.045 | 1.35 | other |
| 2017-08-28 | gmv | daily | category_en=__other__ | 8976 | 2388 | 0.0027 | 9.19 | gmv_spike |
| 2017-08-29 | gmv | daily | total=all | 3.376e+04 | 1.862e+04 | 0.0073 | 4.00 | gmv_spike |
| 2017-08-30 | gmv | daily | total=all | 3.094e+04 | 1.735e+04 | 0.036 | 4.06 | gmv_spike |
| 2017-08-30 | orders | daily | category_en=__other__ | 43 | 22.19 | 0.027 | 3.53 | other |
| 2017-09-01 | gmv | daily | total=all | 2.847e+04 | 1.601e+04 | 0.011 | 3.94 | gmv_spike |
| 2017-09-01 | orders | daily | category_en=__other__ | 32 | 18.43 | 0.046 | 2.55 | other |
| 2017-09-03 | gmv | daily | total=all | 2.78e+04 | 1.278e+04 | 0.00057 | 5.67 | gmv_spike |
| 2017-09-03 | gmv | weekly | customer_state=__other__ | 2.606e+04 | 1.054e+04 | 2.6e-06 | 6.39 | gmv_spike |
| 2017-09-08 | aov | daily | category_en=__other__ | 111.4 | 244.9 | 0.01 | 2.20 | other |
| 2017-09-08 | freight_ratio | daily | total=all | 0.2222 | 0.1533 | 0.01 | 2.13 | other |
| 2017-09-08 | gmv | daily | total=all | 1.188e+04 | 2.545e+04 | 0.01 | 2.53 | other |
| 2017-09-17 | gmv | weekly | category_en=fashion_bags_accessories | 4529 | 2169 | 0.013 | 3.74 | gmv_spike |
| 2017-09-19 | avg_delivery_days | daily | total=all | 27.67 | 11.37 | 0 | 8.09 | other |
| 2017-09-19 | late_delivery_rate | daily | total=all | 0.1879 | 0.02233 | 3.6e-15 | 41.84 | other |
| 2017-09-24 | avg_delivery_days | weekly | category_en=furniture_decor | 19.9 | 11.09 | 1.7e-08 | 3.38 | other |
| 2017-09-24 | gmv | weekly | customer_state=PE | 1714 | 7455 | 0.0004 | 1.85 | other |
| 2017-09-24 | late_delivery_rate | weekly | customer_state=RS | 0.2188 | 0.01996 | 0 | 41.42 | other |
| 2017-09-24 | orders | weekly | customer_state=PE | 11 | 29 | 0.00073 | 1.49 | other |
| 2017-09-29 | gmv | daily | total=all | 3.088e+04 | 1.817e+04 | 0.023 | 3.33 | gmv_spike |
| 2017-10-13 | avg_review_score | daily | total=all | 3.116 | 4.195 | 1.2e-05 | 0.97 | other |
| 2017-10-13 | low_review_rate | daily | total=all | 0.3953 | 0.1241 | 5.6e-08 | 8.22 | other |
| 2017-10-22 | freight_ratio | weekly | seller_tier=unrated | 0.1473 | 0.05591 | 0.0011 | 5.99 | other |
| 2017-10-22 | orders | weekly | category_en=watches_gifts | 99 | 62 | 0.022 | 2.74 | other |
| 2017-10-27 | gmv | daily | seller_tier=T3 | 6583 | 2439 | 0.00022 | 5.78 | gmv_spike |
| 2017-11-03 | avg_review_score | daily | total=all | 2.967 | 4.089 | 2.7e-05 | 0.93 | other |
| 2017-11-03 | low_review_rate | daily | total=all | 0.4333 | 0.1522 | 6.4e-07 | 6.28 | other |
| 2017-11-04 | late_delivery_rate | daily | total=all | 0.122 | 0.03235 | 0.032 | 10.29 | other |
| 2017-11-11 | orders | daily | customer_state=SP | 70 | 43.69 | 0.026 | 2.56 | other |
| 2017-11-12 | orders | weekly | seller_tier=T1 | 74 | 28 | 0.0073 | 7.07 | other |
| 2017-11-16 | orders | daily | payment_type=boleto | 61 | 37.56 | 0.043 | 2.57 | other |
| 2017-11-17 | orders | daily | category_en=__other__ | 40 | 22.6 | 0.043 | 2.84 | other |
| 2017-11-20 | gmv | daily | total=all | 4.428e+04 | 2.922e+04 | 0.018 | 2.79 | gmv_spike |
| 2017-12-01 | gmv | daily | total=all | 3.998e+04 | 2.789e+04 | 0.035 | 2.43 | gmv_spike |
| 2017-12-01 | orders | daily | total=all | 275 | 217.1 | 0.042 | 1.50 | other |
| 2017-12-02 | gmv | daily | customer_state=SP | 1.526e+04 | 8749 | 0.0025 | 3.40 | gmv_spike |
| 2017-12-02 | orders | daily | customer_state=SP | 96 | 67.35 | 0.011 | 1.94 | other |
| 2017-12-04 | gmv | daily | customer_state=RJ | 1.154e+04 | 4773 | 0.00014 | 5.78 | gmv_spike |
| 2017-12-04 | orders | daily | customer_state=RJ | 60 | 38.79 | 0.0019 | 2.24 | other |
| 2017-12-09 | gmv | daily | customer_state=SP | 6401 | 1.133e+04 | 0.045 | 1.79 | other |
| 2017-12-09 | orders | daily | total=all | 155 | 211.4 | 0.045 | 1.35 | other |
| 2017-12-10 | avg_delivery_days | weekly | category_en=housewares | 13.16 | 9.269 | 0.038 | 1.87 | other |
| 2017-12-10 | gmv | weekly | category_en=toys | 2.112e+04 | 1.165e+04 | 9.1e-07 | 4.11 | gmv_spike |
| 2017-12-10 | orders | weekly | category_en=toys | 157 | 69 | 1.2e-11 | 6.45 | other |
| 2017-12-17 | avg_review_score | weekly | category_en=bed_bath_table | 3.408 | 4.076 | 0.0061 | 0.86 | other |
| 2017-12-17 | gmv | weekly | category_en=toys | 1.284e+04 | 2.112e+04 | 0.0025 | 1.81 | other |
| 2017-12-17 | orders | weekly | category_en=toys | 102 | 157 | 0.0073 | 1.62 | other |
| 2017-12-22 | late_delivery_rate | daily | total=all | 0.1822 | 0.0721 | 0.0015 | 8.54 | calendar_window |
| 2017-12-22 | orders | daily | seller_tier=T3 | 12 | 34.13 | 0.0077 | 1.61 | calendar_window |
| 2017-12-23 | late_delivery_rate | daily | total=all | 0.1926 | 0.0721 | 0.00062 | 8.20 | calendar_window |
| 2017-12-24 | avg_delivery_days | weekly | customer_state=MG | 16.4 | 11.24 | 0.0021 | 2.52 | calendar_window |
| 2017-12-24 | avg_review_score | weekly | customer_state=RJ | 3.068 | 4.097 | 7e-05 | 1.45 | calendar_window |
| 2017-12-24 | freight_ratio | daily | total=all | 0.2087 | 0.1582 | 0.048 | 1.30 | calendar_window |
| 2017-12-24 | freight_ratio | weekly | customer_state=RS | 0.1864 | 0.1125 | 0.013 | 2.61 | calendar_window |
| 2017-12-24 | gmv | daily | total=all | 5759 | 2.209e+04 | 0.0026 | 3.01 | calendar_window |
| 2017-12-24 | gmv | weekly | category_en=health_beauty | 8354 | 1.904e+04 | 0.0063 | 2.38 | calendar_window |
| 2017-12-24 | late_delivery_rate | weekly | customer_state=PR | 0.1584 | 0.02275 | 1.1e-13 | 27.52 | calendar_window |
| 2017-12-24 | low_review_rate | weekly | category_en=bed_bath_table | 0.3508 | 0.1342 | 0.011 | 8.90 | calendar_window |
| 2017-12-24 | orders | daily | total=all | 59 | 152.6 | 0.0065 | 2.50 | calendar_window |
| 2017-12-24 | orders | weekly | category_en=perfumery | 35 | 66 | 0.0058 | 1.67 | calendar_window |
| 2017-12-25 | freight_ratio | daily | seller_tier=T4 | 0.2406 | 0.1594 | 0.0032 | 2.17 | calendar_window |
| 2017-12-31 | late_delivery_rate | weekly | customer_state=RJ | 0.3505 | 0.1485 | 0.025 | 7.17 | calendar_window |
