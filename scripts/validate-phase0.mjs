#!/usr/bin/env node
/**
 * Nemoclaw Phase 0 — schema + invariant validation.
 *
 * Usage: npm run validate:phase0
 */
import { readFileSync, existsSync } from "fs";
import path from "path";
import { fileURLToPath } from "url";
import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");

function readJson(rel) {
  return JSON.parse(readFileSync(path.join(ROOT, rel), "utf8"));
}

function assertUserVisibilityInvariant(eventObj) {
  const actions = eventObj.actions ?? [];
  const hasPresent = actions.some((a) => a?.type === "present_to_user");
  if (!hasPresent) return { ok: true };
  const source = eventObj.source_agent;
  if (source === "popeye") return { ok: true };
  return {
    ok: false,
    reason:
      "`present_to_user` is forbidden unless source_agent is popeye (worker direct user path denied).",
  };
}

function compileValidator(schemaPathRelative) {
  const ajv = new Ajv2020({ allErrors: true, strict: true });
  addFormats(ajv);
  const schema = readJson(schemaPathRelative);
  try {
    return ajv.compile(schema);
  } catch (e) {
    throw new Error(`Failed compiling schema ${schemaPathRelative}: ${e.message}`);
  }
}

/** @returns {string[]} errors */
function validatePermissionMatrixCompleteness(matrix) {
  const agents = matrix.agents;
  const errs = [];
  for (const perm of matrix.permissions) {
    const grantKeys = Object.keys(perm.grants).sort();
    const expected = [...agents].sort();
    if (JSON.stringify(grantKeys) !== JSON.stringify(expected))
      errs.push(
        `${perm.permission_id}: grants keys mismatch (have ${grantKeys.join(", ")}, need ${expected.join(", ")})`,
      );
  }
  return errs;
}

/** @returns {string[]} issues for THIS case output */
function lintJoyCaseOutput(c, safetySuite) {
  const text = c.syntheticJoyOutputForUserPackaging ?? "";
  const issues = [];
  const globals = safetySuite.global_blocked_regex?.map((r) => new RegExp(r, "iu")) ?? [];
  const extras = (c.extra_blocked_regex ?? []).map((r) => new RegExp(r, "iu"));
  const markers = c.expect_template_markers_present ?? [];
  for (const m of markers) {
    if (!text.includes(m)) issues.push(`missing marker ${m}`);
  }
  const combined = [...globals, ...extras];
  const hitSources = [];
  for (const r of combined) {
    try {
      if (r.test(text)) hitSources.push(r.source);
    } catch (_) {
      issues.push(`invalid regex in suite: ${r}`);
    }
  }
  if (hitSources.length) issues.push(`blocked-regex: ${hitSources.join("; ")}`);

  const tier = c.tier;
  if ((tier === "watch" || tier === "urgent") && markers.length === 0) {
    const need = tier === "watch" ? "[[JOY_WATCH_V1]]" : "[[JOY_URGENT_V1]]";
    if (!text.includes(need))
      issues.push(`tier=${tier} requires template marker ${need}`);
  }

  return issues;
}

/** @returns {string[]} accumulated errors across suite */
function runSafetyRegression(safetySuite) {
  const errs = [];

  if (!Array.isArray(safetySuite?.cases))
    return ["safety regression: Missing cases array"];

  for (const c of safetySuite.cases) {
    const caseIssues = lintJoyCaseOutput(c, safetySuite);

    const expectFail = c.expect_failure === true;
    if (expectFail) {
      if (caseIssues.length === 0)
        errs.push(`${c.id}: expected failure (violations present) but output looked valid`);
      continue;
    }
    if (caseIssues.length > 0) {
      for (const m of caseIssues) errs.push(`${c.id}: ${m}`);
    }
  }

  return errs;
}

function validateAll() {
  /** @type {string[]} */
  const errors = [];

  const validateEvent = compileValidator(
    "specs/phase0/contracts/event_schema.json",
  );
  const validatePermissions = compileValidator(
    "specs/phase0/contracts/permission_matrix.schema.json",
  );
  const validateTools = compileValidator(
    "specs/phase0/contracts/tool_registry.schema.json",
  );

  const manifest = readJson("specs/phase0/contracts/samples/manifest.json");
  for (const entry of manifest.samples) {
    const rel = path.join("specs/phase0/contracts/samples", entry.file);
    const evt = readJson(rel);
    const okSchema = validateEvent(evt);
    if (!okSchema) {
      errors.push(
        `Schema invalid ${rel}: ${JSON.stringify(validateEvent.errors ?? [], null, 2)}`,
      );
      continue;
    }
    const inv = assertUserVisibilityInvariant(evt);
    if (entry.expectSemanticDeny) {
      if (inv.ok)
        errors.push(
          `Semantic deny missing for ${rel} — expected invariant violation.`,
        );
    } else if (!inv.ok) {
      errors.push(`Semantic fail ${rel}: ${inv.reason}`);
    }
  }

  const permMat = readJson("specs/phase0/contracts/permission_matrix.json");
  if (!validatePermissions(permMat)) {
    errors.push(
      `permission_matrix invalid: ${JSON.stringify(validatePermissions.errors, null, 2)}`,
    );
  } else {
    errors.push(...validatePermissionMatrixCompleteness(permMat));
  }

  const registry = readJson("specs/phase0/contracts/tool_registry.json");
  if (!validateTools(registry)) {
    errors.push(
      `tool_registry invalid: ${JSON.stringify(validateTools.errors, null, 2)}`,
    );
  }

  const safetyRegression = readJson("specs/phase0/safety/safety_regression_cases.json");
  const joys = readJson("specs/phase0/safety/joy_templates.json");

  errors.push(...runSafetyRegression(safetyRegression).map((m) => `safety regression: ${m}`));

  if (existsSync(path.join(ROOT, "specs/phase0/safety/joy_disclaimer_templates.json"))) {
    const dup = readJson("specs/phase0/safety/joy_disclaimer_templates.json");
    if (JSON.stringify(dup.templates) !== JSON.stringify(joys.templates))
      errors.push(
        "joy_disclaimer_templates.json must mirror joy_templates.json templates array.",
      );
  }

  const joyRules = readJson("specs/phase0/safety/joy_escalation_rules.json");
  for (const r of joyRules.rules) {
    if (!["info", "watch", "urgent"].includes(r.tier))
      errors.push(`joy_escalation_rules: invalid tier ${r.id} -> ${r.tier}`);
    for (const t of r.required_templates ?? []) {
      if (!joys.templates.some((jt) => jt.id === t))
        errors.push(`joy_escalation_rules: missing template reference ${t} in ${r.id}`);
    }
  }

  return errors;
}

const errs = validateAll();
if (errs.length) {
  console.error("Phase 0 validation FAILED:");
  for (const e of errs) console.error(" - ", e);
  process.exit(1);
}
console.log("Phase 0 validation OK.");
