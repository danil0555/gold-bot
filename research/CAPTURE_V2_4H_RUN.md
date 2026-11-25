# Capture-scoped v2 four-hour run

This branch contains an isolated GitHub Actions workflow for a four-hour, read-only public BingX capture into a disposable SQLite database. It does not access production data, private account endpoints, credentials, or place orders.

The workflow validates the capture database before generating exploratory event-study diagnostics. Results must be interpreted as research only; four hours do not establish profitability or a validated trading edge.
