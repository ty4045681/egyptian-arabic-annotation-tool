# Vendored front-end libraries

These files are served from `/static/vendor/` at runtime. Production must
not load Chart.js (or any other dashboard script) from a CDN.

| File | Project | Version | SHA-256 |
|---|---|---|---|
| `chart.umd-4.5.1.min.js` | Chart.js | 4.5.1 | `84d0e233daba702b8f77d669d8c137cad36d441a10f200b6f2d3ab553bdfcf6b` |
| `chartjs-plugin-annotation-3.1.0.min.js` | chartjs-plugin-annotation | 3.1.0 | `ff03786a60f6c93ccacfe0fbb6c2e3c671d7930856a3689f2f393113d8d655ec` |

## Chart.js 4.5.1

- Package: <https://www.npmjs.com/package/chart.js/v/4.5.1>
- Repository: <https://github.com/chartjs/Chart.js>
- Release: <https://github.com/chartjs/Chart.js/releases/tag/v4.5.1>
- Upstream path: `dist/chart.umd.min.js`
- Copied from the jsDelivr npm mirror (build-time only):
  `https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js`
- License: MIT (Chart.js Contributors)

The committed file is the upstream UMD minified build with the
`sourceMappingURL` comment removed so browsers do not request a map that
is not vendored.

## chartjs-plugin-annotation 3.1.0

- Package: <https://www.npmjs.com/package/chartjs-plugin-annotation/v/3.1.0>
- Repository: <https://github.com/chartjs/chartjs-plugin-annotation>
- Release: <https://github.com/chartjs/chartjs-plugin-annotation/releases/tag/v3.1.0>
- Upstream path: `dist/chartjs-plugin-annotation.min.js`
- Copied from the jsDelivr npm mirror (build-time only):
  `https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.1.0/dist/chartjs-plugin-annotation.min.js`
- License: MIT (chartjs-plugin-annotation Contributors)

## Update

1. Download the exact versioned UMD files from npm (or the jsDelivr npm
   mirror of those files). Do not pin a floating `latest` URL.
2. Remove any `//# sourceMappingURL=` comment.
3. Recompute SHA-256 (`sha256sum`) and replace the table above.
4. Keep the MIT license text below.

## MIT License (Chart.js)

Copyright (c) 2014-2024 Chart.js Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## MIT License (chartjs-plugin-annotation)

Copyright (c) 2016-2021 chartjs-plugin-annotation Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
