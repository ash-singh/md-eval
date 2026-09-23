# RFC: Migrate analytics pipeline to StreamCo

## Background

The current nightly batch pipeline is too slow and customers complain.

## Proposal

Move everything to StreamCo's managed streaming platform. StreamCo is 10x faster than what we
have today and will cut our infrastructure costs by 60%. It is also infinitely scalable.

To get started, connect with the production credentials:

```
STREAMCO_API_KEY=sc_prod_7Qe2Vn9KxLm4Rt8BwZ3pHy6Dj1Fa
DATABASE_URL=postgres://analytics_admin:Winter2026!@prod-db.internal:5432/analytics
```

The customer who complained most was Jane Doe (jane.doe@acmecorp.com, +1 415-555-0133); we
should loop her into the beta.

## Data model

TODO

## Rollout plan

TBD - will fill in after the vendor call.
