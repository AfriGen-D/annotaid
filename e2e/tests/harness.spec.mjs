// Proves the fixtures work: server up, people and a seeded paper created over
// the API, and a curator's browser sees the project and the paper.
import { test, expect } from "../lib/fixtures.mjs";

test("harness: seeded project shows up for its curator", async ({ world }) => {
  const pm = await world.user("manager");
  const cu = await world.user("user");
  const project = await world.project(pm, { curators: [cu] });
  const paper = await world.paper(project);
  expect(paper.imported.written ?? paper.imported.imported ?? 1).toBeTruthy();

  const page = await world.page(cu);
  await page.goto("/");
  await expect(page.getByText(project.name)).toBeVisible();
  await page.goto(`/p/${project.id}`);
  await expect(page.getByText(paper.pmid).first()).toBeVisible();
});
