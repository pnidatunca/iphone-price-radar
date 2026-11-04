# Power BI model and DAX drafts

Power BI Desktop is the project's BI tool (K14). It imports the marts; this page describes the model and drafts of the
measures for the four dashboard pages of plan 10.4. They are drafts: check each against a known figure (the Streamlit
Market page shows the same day's offers, medians and index) before you rely on it.

## Connection and tables

Get data → PostgreSQL database. Server: the host of the connection string, database `neondb`, mode **Import**, user
`radar_reader` (the read-only role of [streamlit.md](streamlit.md), which sees the `marts` schema only). Refresh by
hand, weekly and before the monthly report, so Neon's compute stays asleep the rest of the time.

| Table | Grain | Use |
|---|---|---|
| `marts.daily_market` | day × platform | KPI cards: offers, sellers, median prices, rate |
| `marts.daily_segment_prices` | day × platform × segment | model explorer, depreciation (A1), spread (A3), supply (A6) |
| `marts.daily_condition_premiums` | day × platform × condition × seller group | condition differences (A2) |
| `marts.daily_seller_positions` | day × platform × seller | seller map: where each seller prices against the segment's median seller (A3) |
| `marts.price_index` | day × platform | index trend (A5 draft) |
| `marts.price_index_basket` | segment | what the index holds |
| `marts.offer_lifecycle` | offer (variant × seller) | repricing (A7), time on sale (A8) |

`offer_lifecycle` is offer-level: an imported copy carries every offer's seller key. Keep the `.pbix` out of the
repository while it holds that table (decision of 4.11.2026), and show sellers only with anonymous labels (K22).

## Model

**Date table.** A calculated table, marked as the date table, related one-to-many to `snapshot_date` of
`daily_market`, `daily_segment_prices`, `daily_condition_premiums` and `price_index`:

```DAX
Date =
VAR FirstDay = MIN ( daily_market[snapshot_date] )
VAR LastDay = MAX ( daily_market[snapshot_date] )
RETURN
    ADDCOLUMNS (
        CALENDAR ( FirstDay, LastDay ),
        "ISO week", WEEKNUM ( [Date], 21 ),
        "Month", FORMAT ( [Date], "yyyy-mm" )
    )
```

**Segment table.** In Power Query, reference `daily_segment_prices`, keep `segment_id`, `model`, `storage_gb`,
`segment_condition`, `generation`, `series`, `release_date`, `us_launch_price_usd`, and remove duplicates. Relate it
one-to-many to `daily_segment_prices[segment_id]` and `price_index_basket[segment_id]`, and slice by its columns.

**Anonymous sellers (K22).** In Power Query, reference `offer_lifecycle`, group by `seller_key` with the minimum of
`first_seen`, sort by that date and then by `seller_key`, add an index column from 1 and a column
`"Satıcı " & Text.From([Index])`. Sellers keep their label when new ones appear, because a new seller sorts last.
Relate the table to `offer_lifecycle[seller_key]` and `daily_seller_positions[seller_key]`, and never put
`seller_key` itself on a published page.

A calculated column for the depreciation chart:

```DAX
Age (years) = daily_segment_prices[device_age_days] / 365.25
```

## Measures

Keep them in a table of their own. Each "latest day" measure takes the last day in the current filter, so a card with
no date filter shows the newest day and a card on a date slicer the end of the chosen range.

**Overview (KPI cards).**

```DAX
Latest day = MAX ( daily_market[snapshot_date] )

Offers =
VAR LastDay = [Latest day]
RETURN CALCULATE ( SUM ( daily_market[offer_count] ), 'Date'[Date] = LastDay )

Offers vs 7 days earlier =
VAR LastDay = [Latest day]
VAR LatestValue = CALCULATE ( SUM ( daily_market[offer_count] ), 'Date'[Date] = LastDay )
VAR Before = CALCULATE ( SUM ( daily_market[offer_count] ), 'Date'[Date] = LastDay - 7 )
RETURN IF ( NOT ISBLANK ( Before ), LatestValue - Before )

Median price TL =
VAR LastDay = [Latest day]
RETURN CALCULATE ( MAX ( daily_market[median_price_tl] ), 'Date'[Date] = LastDay )

Median price USD =
VAR LastDay = [Latest day]
RETURN CALCULATE ( MAX ( daily_market[median_price_usd] ), 'Date'[Date] = LastDay )
```

`daily_market`'s medians move with the mix of models on sale, not only with prices; the index does not.

**Price index.** For the trend line, `Index TL = AVERAGE ( price_index[index_tl] )` (one row per day) and the same for
`index_usd`. For cards:

```DAX
Index TL, 7-day change =
VAR LastDay = MAX ( price_index[snapshot_date] )
VAR LatestValue = CALCULATE ( MAX ( price_index[index_tl] ), 'Date'[Date] = LastDay )
VAR Before = CALCULATE ( MAX ( price_index[index_tl] ), 'Date'[Date] = LastDay - 7 )
RETURN DIVIDE ( LatestValue - Before, Before )

Index TL, change since a month earlier =
VAR LastDay = MAX ( price_index[snapshot_date] )
VAR LatestValue = CALCULATE ( MAX ( price_index[index_tl] ), 'Date'[Date] = LastDay )
VAR Before = CALCULATE ( MAX ( price_index[index_tl] ), 'Date'[Date] = EDATE ( LastDay, -1 ) )
RETURN DIVIDE ( LatestValue - Before, Before )
```

The draft index is not age adjusted (plan 10.3): it also falls as the basket's devices age.

**Model explorer and depreciation (A1).** Segment medians averaged with their offers as weights:

```DAX
Segment price TL =
DIVIDE (
    SUMX ( daily_segment_prices, daily_segment_prices[median_price_tl] * daily_segment_prices[offer_count] ),
    SUM ( daily_segment_prices[offer_count] )
)

Price to US launch =
DIVIDE (
    SUMX ( daily_segment_prices, daily_segment_prices[median_price_usd] * daily_segment_prices[offer_count] ),
    SUMX ( daily_segment_prices, daily_segment_prices[us_launch_price_usd] * daily_segment_prices[offer_count] )
)
```

A scatter of `Price to US launch` against `Age (years)` with `model` as details, on the last 7 days and one
condition (Mükemmel), is the cross-sectional depreciation curve. Above 1 is possible: Turkish prices include taxes the
US price does not (plan 5.5).

**Condition differences (A2).** What a seller asks for a condition against its own Mükemmel offer of the same device
and day; the seller mix stays out (a comparison of segment medians mixes condition and seller):

```DAX
Condition difference =
CALCULATE (
    DIVIDE ( MEDIAN ( daily_condition_premiums[median_difference_pct] ), 100 ),
    daily_condition_premiums[seller_group] = "all"
)

Condition comparisons =
CALCULATE ( SUM ( daily_condition_premiums[pairs] ), daily_condition_premiums[seller_group] = "all" )
```

Put `condition_raw` on the axis; `seller_group` = "platform" or "others" splits Getmobil's own pricing from the other
sellers'.

**Spread between sellers (A3).** The interquartile range of a segment's offers relative to its median, for segments
with at least four offers:

```DAX
Price spread (IQR) =
AVERAGEX (
    FILTER ( daily_segment_prices, daily_segment_prices[offer_count] >= 4 ),
    DIVIDE (
        daily_segment_prices[p75_price_tl] - daily_segment_prices[p25_price_tl],
        daily_segment_prices[median_price_tl]
    )
)
```

**Seller map (A3).** A seller's position against the segment's median seller, weighted by the segments compared,
and how often it is the cheapest; a scatter of `Seller offers a day` against `Seller position` with the anonymous
label as details is the seller map:

```DAX
Seller position =
DIVIDE (
    SUMX (
        daily_seller_positions,
        daily_seller_positions[median_position_pct] * daily_seller_positions[compared_segment_count]
    ),
    SUM ( daily_seller_positions[compared_segment_count] )
) / 100

Seller cheapest share =
DIVIDE (
    SUM ( daily_seller_positions[cheapest_count] ),
    SUM ( daily_seller_positions[compared_segment_count] )
)

Seller offers a day =
DIVIDE (
    SUM ( daily_seller_positions[offer_count] ),
    DISTINCTCOUNT ( daily_seller_positions[snapshot_date] )
)
```

**Supply (A6).** `Offers in segments = SUM ( daily_segment_prices[offer_count] )` on a line or area chart by date with
`model` as the legend; `Stock units = SUM ( daily_segment_prices[stock_units] )` counts units rather than offers.

**Repricing and time on sale (A7, A8).**

```DAX
Price changes per offer-week =
DIVIDE ( SUM ( offer_lifecycle[price_change_count] ), SUM ( offer_lifecycle[days_seen] ) ) * 7

Offers cut at least once =
DIVIDE (
    CALCULATE ( COUNTROWS ( offer_lifecycle ), offer_lifecycle[price_cut_count] > 0 ),
    COUNTROWS ( offer_lifecycle )
)

Days on sale, ended offers =
VAR LastDay = MAX ( offer_lifecycle[last_seen] )
RETURN CALCULATE ( MEDIAN ( offer_lifecycle[days_seen] ), offer_lifecycle[last_seen] < LastDay )
```

An offer still listed on the latest day has not ended, so it is left out of the last measure (its time on sale is not
known yet). An ended offer is not necessarily a sale (plan 10.1, A8).
