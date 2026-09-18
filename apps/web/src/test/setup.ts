import { afterEach } from "vitest"
import { cleanup } from "@testing-library/react"
import "@testing-library/jest-dom/vitest"

// `@testing-library/react` only self-registers its cleanup when `afterEach` is a
// global, and this config runs vitest without `globals: true`. Without the line
// below the rendered tree is never unmounted, so a later test can still see an
// earlier test's DOM — which makes every "is not rendered" assertion pass for the
// wrong reason.
afterEach(cleanup)

// jsdom implements no layout, so `scrollIntoView` is missing entirely. The review
// screen calls it when an evidence chunk is located; without the stub that would
// fail the test instead of the behaviour being absent.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
