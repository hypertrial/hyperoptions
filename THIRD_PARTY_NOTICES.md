# Third-party notices

The project-owned HyperOptions and integrated StockSweeper source is licensed under
the repository's MIT license. Dependencies retain their own licenses.

Option-chain fallback, dividend checks, and provisional option-expiry observations use Yahoo Finance through
[yfinance](https://github.com/ranaroussi/yfinance). yfinance describes use of Yahoo's
API as intended for personal use and directs users to Yahoo's data terms. The app
is a local personal workstation; downloaded market data is stored locally and is
not included in this repository or its releases.

Market-implied odds use [regimelib](https://github.com/microprediction/regimelib)
0.1.0 to fit and price a two-state model. The app checks pricing convergence and
withholds unstable results because of a [known numerical pricing issue](https://github.com/microprediction/regimelib/issues/2).
The rate curve comes from the [U.S. Treasury daily feed](https://home.treasury.gov/treasury-daily-interest-rate-xml-feed).
