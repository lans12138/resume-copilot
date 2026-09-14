import "@testing-library/jest-dom/vitest"

// jsdom implements no layout, so `scrollIntoView` is missing entirely. The review
// screen calls it when an evidence chunk is located; without the stub that would
// fail the test instead of the behaviour being absent.
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
