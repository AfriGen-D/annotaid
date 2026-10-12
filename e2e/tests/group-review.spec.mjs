import { test, expect } from "../lib/fixtures.mjs";

test("schema display names generate collision-safe field names", async ({ world }) => {
  const manager = await world.user("manager");
  const page = await world.page(manager);
  await page.goto("/");
  await page.locator("#newProjectBtn").click();
  await page.getByPlaceholder("e.g. GWAS pilot").fill("Generated names");
  await page.getByRole("button", { name: "Nothing — I'll define it" }).click();

  const displays = page.locator(".fe-card:not(.fe-group) .fe-label");
  const keys = page.locator(".fe-card:not(.fe-group) .fe-name");
  await displays.first().fill("Primary P-value");
  await expect(keys.first()).toHaveValue("primary_p_value");

  await page.getByRole("button", { name: "+ Feature", exact: true }).click();
  await displays.nth(1).fill("Primary P-value");
  await expect(keys.nth(1)).toHaveValue("primary_p_value_2");
});

test("assignee can locate group evidence and approved panel disappears", async ({ world }) => {
  const manager = await world.user("manager");
  const curator = await world.user("user");
  const project = await world.project(manager, { curators: [curator] });
  const paper = await world.paper(project);
  await world.assign(project, paper, curator);

  const curatorApi = await world.api(curator);
  const saved = await curatorApi.ok(
    "POST",
    `/api/projects/${project.id}/papers/${paper.uid}/group-items`,
    {
      items: [
        { label: "rs1801133", evidence: "The lead association reached p = 5.2e-8 at rs1801133 (MTHFR)." },
        { label: "RS1801133", evidence: "duplicate spelling" },
      ],
    },
  );
  expect(saved.items).toHaveLength(1);

  const page = await world.page(curator);
  await page.goto(`/p/${project.id}`);
  await expect(page.locator(".gi-panel")).toBeVisible();
  await expect(page.locator(".gi-row .gi-label-input")).toHaveCount(1);
  const labelBox = await page.locator(".gi-panel .gi-label-input").boundingBox();
  const deleteBox = await page.locator(".gi-panel .gi-row .fe-del").boundingBox();
  expect(Math.abs((labelBox.y + labelBox.height / 2) -
    (deleteBox.y + deleteBox.height / 2))).toBeLessThan(6);
  await page.locator(".gi-evidence").click();
  await expect(page.locator("#navlabel")).toContainText("rs1801133");
  await expect(page.locator(".hlLayer .hl")).toBeVisible();

  await page.locator(".gi-panel .gi-add").click();
  const added = page.locator(".gi-panel .gi-label-input").last();
  await added.fill("EVA:eva123");
  const addSaved = page.waitForResponse(response =>
    response.url().includes("/group-items") && response.request().method() === "POST");
  await added.press("Tab");
  await addSaved;
  await expect(page.locator(".gi-panel .gi-label-input")).toHaveCount(2);

  await page.locator(".gi-panel .gi-row .fe-del").last().click();
  await expect(page.locator(".dlg-overlay")).toContainText("Remove");
  const removeSaved = page.waitForResponse(response =>
    response.url().includes("/group-items") && response.request().method() === "POST");
  await page.locator(".dlg-overlay").getByRole("button", { name: "Remove identifier" }).click();
  await removeSaved;
  await expect(page.locator(".gi-panel .gi-label-input")).toHaveCount(1);

  await page.locator(".gi-panel .gi-add").click();
  const second = page.locator(".gi-panel .gi-label-input").last();
  await second.fill("rs2222");
  const secondSaved = page.waitForResponse(response =>
    response.url().includes("/group-items") && response.request().method() === "POST");
  await second.press("Tab");
  await secondSaved;
  await page.locator(".gi-panel .gi-select-all input").check();
  await expect(page.locator(".gi-panel .gi-remove-selected")).toContainText("(2)");
  await page.locator(".gi-panel .gi-remove-selected").click();
  await expect(page.locator(".dlg-overlay")).toContainText("2 selected identifiers");
  const bulkSaved = page.waitForResponse(response =>
    response.url().includes("/group-items") && response.request().method() === "POST");
  await page.locator(".dlg-overlay").getByRole("button", { name: "Remove 2 identifiers" }).click();
  await bulkSaved;
  await expect(page.locator(".gi-panel .gi-label-input")).toHaveCount(0);

  await curatorApi.ok(
    "POST",
    `/api/projects/${project.id}/papers/${paper.uid}/group-items`,
    { items: saved.items, reviewed: true },
  );
  await page.reload();
  await expect(page.locator(".gi-panel")).toHaveCount(0);

  const managerPage = await world.page(manager);
  await managerPage.goto(`/p/${project.id}`);
  await managerPage.evaluate(async ({ uid, pmid, items }) => {
    const { openAddPapersModal } = await import("/js/addPapersModal.js");
    const current = {
      uid, pmid, filename: `${pmid}.pdf`, pmidStatus: "confirmed",
      groupItemsDiscovered: true, groupItemsReviewed: false, groupItems: items,
    };
    openAddPapersModal({
      onStartImport: async () => ({}), onRetryJob: async () => ({}),
      onCancelJob: async () => ({}), resolvePaper: async () => current,
      onUpload: async () => [], onConfirmPmid: async () => current,
      onNoPmid: async () => current, onStartExtract: async () => ({}),
      onSaveGroupItems: async (_uid, next) => ({
        items: next.filter(item => item.label.trim()).map((item, index) => ({
          ...item, rowId: item.rowId || `manual-${index}`,
        })),
      }),
      onFinish: () => {}, findByPmid: () => null, canExtract: true,
      config: {
        features: [{ name: "variants", label: "Variants", type: "group", features: [] }],
        defaults: { model: "test" },
      },
      bulk: { papers: () => [current], hasRun: () => false, currentUid: uid },
    });
  }, { uid: paper.uid, pmid: paper.pmid, items: saved.items });
  await expect(managerPage.locator("#addPdfModal .modal-head")).toContainText("Identify groups");
  await expect(managerPage.locator(".group-preview-card .gi-label-input")).toHaveValue("rs1801133");

  await managerPage.locator(".group-preview-card .gi-add").click();
  const modalAdded = managerPage.locator(".group-preview-card .gi-label-input").last();
  await modalAdded.fill("rs2222");
  await modalAdded.press("Tab");
  await expect(managerPage.locator(".group-preview-message")).toHaveText("Saved");
  await expect(managerPage.locator(".group-preview-card .gi-label-input")).toHaveCount(2);

  await managerPage.locator(".group-preview-card .gi-row .fe-del").last().click();
  await expect(managerPage.locator(".dlg-overlay")).toContainText("identified groups");
  await managerPage.locator(".dlg-overlay").getByRole("button", { name: "Remove identifier" }).click();
  await expect(managerPage.locator(".group-preview-card .gi-label-input")).toHaveCount(1);
});

test("deleting a post-extraction group warns when it has curated values", async ({ world }) => {
  const manager = await world.user("manager");
  const page = await world.page(manager);
  await page.goto("/");

  await page.evaluate(async () => {
    const { renderGroup } = await import("/js/groupEditor.js");
    const host = document.createElement("div");
    host.id = "curated-delete-test";
    document.body.appendChild(host);
    host.appendChild(renderGroup(
      {
        name: "variants", label: "Variants", type: "group", maxItems: 20,
        features: [{ name: "p_value", label: "P value", type: "number" }],
      },
      {
        present: true, confirmed: false,
        rows: [{
          rowId: "row-1", origin: "curator", deletedAt: null,
          features: {
            p_value: {
              type: "number", aiValue: null, value: 0.01, present: true,
              evidence: null, confirmed: true, confirmedAt: "now", editedAt: "now",
            },
          },
        }],
      },
      {
        openRowId: null, onOpenRow: () => {}, onEdit: () => {},
        onActivateEvidence: () => {}, groupItemLabels: { "row-1": "rs123" },
        onRemoveDeclaredItem: async () => {},
        onStructureChange: () => { host.dataset.changed = "yes"; },
        readOnly: false, users: {},
      },
    ));
  });

  await page.locator("#curated-delete-test .grp-row .fe-del").click();
  await expect(page.locator(".dlg-overlay")).toContainText("already contains curated or confirmed values");
  await page.locator(".dlg-overlay").getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator("#curated-delete-test .grp-row")).toHaveCount(1);
  await expect(page.locator("#curated-delete-test")).not.toHaveAttribute("data-changed", "yes");
  await page.locator("#curated-delete-test .grp-row .fe-del").click();
  await page.locator(".dlg-overlay").getByRole("button", { name: "Remove entry" }).click();
  await expect(page.locator("#curated-delete-test")).toHaveAttribute("data-changed", "yes");
});

test("adding a group entry uses an app prompt with cancellation and required input", async ({ world }) => {
  const manager = await world.user("manager");
  const page = await world.page(manager);
  await page.goto("/");
  await page.evaluate(async () => {
    const { renderGroup } = await import("/js/groupEditor.js");
    const host = document.createElement("div");
    host.id = "group-prompt-test";
    document.body.appendChild(host);
    host.appendChild(renderGroup(
      { name: "variants", type: "group", features: [] },
      { present: false, rows: [] },
      { openRowId: null, onOpenRow: () => {}, onEdit: () => {},
        onStructureChange: () => { host.dataset.added = "yes"; },
        onActivateEvidence: () => {}, onDeclareItem: async label => {
          host.dataset.label = label;
          return "new-row";
        },
        groupItemLabels: {}, readOnly: false, users: {} },
    ));
  });

  await page.locator("#group-prompt-test .grp-add").click();
  await expect(page.locator(".dlg-overlay")).toContainText("Identify this entry");
  await page.locator(".dlg-overlay").getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator("#group-prompt-test")).not.toHaveAttribute("data-added", "yes");

  await page.locator("#group-prompt-test .grp-add").click();
  await page.locator(".dlg-overlay").getByRole("button", { name: "Add entry" }).click();
  await expect(page.locator(".dlg-overlay .dlg-err")).toBeVisible();
  await page.locator(".dlg-overlay .dlg-input").fill("rs789");
  await page.locator(".dlg-overlay .dlg-input").press("Enter");
  await expect(page.locator("#group-prompt-test")).toHaveAttribute("data-label", "rs789");
  await expect(page.locator("#group-prompt-test")).toHaveAttribute("data-added", "yes");
});

test("variant identifiers show a working source icon only when discovery evidence exists", async ({ world }) => {
  const manager = await world.user("manager");
  const page = await world.page(manager);
  await page.goto("/");

  await page.evaluate(async () => {
    const { renderGroup } = await import("/js/groupEditor.js");
    const host = document.createElement("div");
    host.id = "identifier-evidence-test";
    document.body.appendChild(host);
    host.appendChild(renderGroup(
      { name: "variants", type: "group", features: [] },
      { present: true, rows: [
        { rowId: "with-source", origin: "ai", features: {} },
        { rowId: "without-source", origin: "ai", features: {} },
      ] },
      {
        openRowId: null, onOpenRow: () => { host.dataset.opened = "yes"; },
        onEdit: () => {}, onStructureChange: () => {},
        onActivateEvidence: key => { host.dataset.evidence = key; },
        groupItemLabels: { "with-source": "rs123", "without-source": "rs456" },
        groupItemEvidence: { "with-source": "The rs123 variant was reported." },
        readOnly: true, users: {},
      },
    ));
  });

  const rows = page.locator("#identifier-evidence-test .grp-row");
  await expect(rows.first().locator(".rl-evidence")).toBeVisible();
  await expect(rows.last().locator(".rl-evidence")).toHaveCount(0);
  const buttonBox = await rows.first().locator(".rl-evidence").boundingBox();
  const iconBox = await rows.first().locator(".rl-evidence svg").boundingBox();
  expect(Math.abs((buttonBox.x + buttonBox.width / 2) -
    (iconBox.x + iconBox.width / 2))).toBeLessThan(1);
  expect(Math.abs((buttonBox.y + buttonBox.height / 2) -
    (iconBox.y + iconBox.height / 2))).toBeLessThan(1);
  await rows.first().locator(".rl-evidence").click();
  await expect(page.locator("#identifier-evidence-test"))
    .toHaveAttribute("data-evidence", "group-item@with-source");
  await expect(page.locator("#identifier-evidence-test")).not.toHaveAttribute("data-opened", "yes");
});
