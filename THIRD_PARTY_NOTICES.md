# Third-party notices

Nexus Harness's private Windows runtime includes `langsmith` 0.11.1, used by
LangGraph. The package metadata declares the MIT license and identifies the
upstream repository as <https://github.com/langchain-ai/langsmith-sdk>.

## langsmith — MIT License

Copyright (c) LangChain, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Bundled Node.js and Playwright verification runtime

The Windows installer includes Node.js 22.18.0, Playwright 1.62.1,
Playwright Core 1.62.1, `@playwright/test` 1.62.1, and Chromium Headless Shell
151.0.7922.34 (Playwright revision 1234). They are used only to run genuine
browser verification in Nexus's disposable Windows AppContainer.

- Node.js is distributed under the MIT License and includes third-party
  software under the terms collected in `runtime/playwright/NODE_LICENSE`.
- Playwright, Playwright Core, and `@playwright/test` are distributed under
  the Apache License 2.0. Each package's `LICENSE`, `NOTICE`, and
  `ThirdPartyNotices.txt` files are preserved under
  `runtime/playwright/node_modules`.
- Chromium Headless Shell is distributed under the Chromium project's BSD
  license and other compatible open-source licenses. Its complete generated
  license and third-party notices are preserved as
  `runtime/playwright/browsers/chromium_headless_shell-1234/`
  `chrome-headless-shell-win64/LICENSE.headless_shell`.

The exact download URLs, package integrity values, versions, revisions, and
archive SHA-256 values used by the reproducible runtime builder are recorded
in `runtime-playwright.lock.json` in the Nexus Harness source distribution.
