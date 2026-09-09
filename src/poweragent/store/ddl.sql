-- poweragent/store/ddl.sql
-- SQLite physical schema · design.md §5.1 · 8 主要实体 + 5 张支撑表 = 13 张表
-- 全文事实的唯一写入目标。status 列只存在于 runs 表。

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE tasks (
  task_id              TEXT PRIMARY KEY,
  simulation_only      INTEGER NOT NULL,
  task_kind            TEXT NOT NULL,          -- optimize | dense_grid | robustness | baseline_gate
  model_package_hash   TEXT NOT NULL,
  metrics_hash         TEXT NOT NULL,
  constraints_hash     TEXT NOT NULL,
  scenario_set_hash    TEXT NOT NULL,
  execution_env_hash   TEXT NOT NULL,
  calibration_hash     TEXT NOT NULL DEFAULT '',  -- 绑定 configs/calibration.yaml 与数据集划分记录
                                                  -- PoC 轨为空串；工程轨 M2 之后非空
  budget_max_starts    INTEGER NOT NULL,
  started_at           TEXT NOT NULL,
  ended_at             TEXT,
  stop_reason          TEXT,                   -- budget_exhausted | stop_and_ask_human
                                                 -- | no_improvement | target_reached
  cause                TEXT                    -- stop_reason 的伴随 cause，重启后可回溯
);

CREATE TABLE scenario_set (                    -- 冻结的显式场景行
  task_id       TEXT NOT NULL REFERENCES tasks(task_id),
  scenario_id   TEXT NOT NULL,
  tier          TEXT NOT NULL CHECK (tier IN ('screening','evaluation','robustness')),
  model_variant TEXT NOT NULL CHECK (model_variant IN ('switching','averaged')),
  require_margin INTEGER NOT NULL,
  spec_json     TEXT NOT NULL,                 -- vin/temp/load/slew 规范化 JSON
  spec_version  TEXT NOT NULL,
  PRIMARY KEY (task_id, scenario_id)
);

CREATE TABLE candidates (
  candidate_id       TEXT PRIMARY KEY,         -- sha256(parameters_si 规范化 JSON)[:16]
  task_id            TEXT NOT NULL REFERENCES tasks(task_id),
  parameters_si      TEXT NOT NULL,            -- {"rcomp":11000.0,"ccomp":2.3e-9}
  origin             TEXT NOT NULL CHECK (origin IN ('agent','baseline','dense_grid','manual')),
  origin_llm_call_id TEXT REFERENCES llm_calls(llm_call_id),
  round_index        INTEGER,
  created_at         TEXT NOT NULL
);

CREATE TABLE runs (                            -- 粒度 (candidate_id, scenario_id, attempt)
  run_id           TEXT PRIMARY KEY,
  task_id          TEXT NOT NULL REFERENCES tasks(task_id),
  candidate_id     TEXT NOT NULL REFERENCES candidates(candidate_id),
  scenario_id      TEXT NOT NULL,
  attempt          INTEGER NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('running','waiting','done','failed')),
  failure_class    TEXT CHECK (failure_class IN ('candidate_rejected','transient_error','stop_and_ask_human','budget_exhausted')),
  cause            TEXT,
  simulation_key   TEXT NOT NULL,
  evaluation_key   TEXT,
  budget_units     INTEGER NOT NULL DEFAULT 0, -- 真实 engine 启动数；缓存命中为 0
  cache_hit        INTEGER NOT NULL DEFAULT 0,
  waveform_ref     TEXT,
  observable_ref   TEXT,
  elapsed_ms       INTEGER,
  started_at       TEXT NOT NULL,
  ended_at         TEXT,
  UNIQUE (task_id, candidate_id, scenario_id, attempt)
);
CREATE INDEX idx_runs_simkey ON runs(simulation_key);
CREATE INDEX idx_runs_cand   ON runs(task_id, candidate_id, status);

CREATE TABLE metric_results (
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  metric_id      TEXT NOT NULL,
  value          REAL,
  valid          INTEGER NOT NULL,
  invalid_reason TEXT,
  evaluation_key TEXT NOT NULL,
  PRIMARY KEY (run_id, metric_id)
);

CREATE TABLE constraint_results (
  candidate_id   TEXT NOT NULL REFERENCES candidates(candidate_id),
  scenario_id    TEXT NOT NULL,
  run_id         TEXT NOT NULL REFERENCES runs(run_id),
  feasible       INTEGER NOT NULL,
  violations     TEXT NOT NULL,                -- JSON 数组：[{constraint,limit,actual,unit}]
  evaluation_key TEXT NOT NULL,
  PRIMARY KEY (candidate_id, scenario_id, run_id)
);

CREATE TABLE approvals (
  approval_id   TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(task_id),
  kind          TEXT NOT NULL,                 -- checkpoint1 | budget_increase | final_recommendation
                                                 -- | track_selection | m5_assessment
                                                 -- 自由文本列，无 CHECK 约束
  candidate_id  TEXT REFERENCES candidates(candidate_id),
  result_hash   TEXT NOT NULL,                 -- 绑定不可变结果
  approver      TEXT NOT NULL,
  second_approver TEXT,                        -- 仅验收轨道与最终推荐双签
  decision      TEXT NOT NULL CHECK (decision IN ('approve','reject')),
  extra_units   INTEGER,                       -- 仅 kind='budget_increase' 非空；追加的 engine 启动额度
  note          TEXT,
  created_at    TEXT NOT NULL
);

CREATE TABLE interventions (                   -- 埋点，M1 起连续
  intervention_id TEXT PRIMARY KEY,
  task_id      TEXT NOT NULL REFERENCES tasks(task_id),
  checkpoint   TEXT NOT NULL CHECK (checkpoint IN ('cp1','cp2','cp3','other')),
  reason       TEXT NOT NULL,
  action       TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

CREATE TABLE llm_calls (                       -- 六字段
  llm_call_id TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL REFERENCES tasks(task_id),
  role        TEXT NOT NULL,                   -- 阶段1 仅 'proposal'
  model_id    TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  context_hash TEXT NOT NULL,
  tokens      INTEGER NOT NULL,
  outcome     TEXT NOT NULL,                   -- ok | schema_invalid | repaired | empty
  evidence_ids TEXT,                           -- JSON 数组
  created_at  TEXT NOT NULL
);

CREATE TABLE evidence (
  evidence_id TEXT PRIMARY KEY,
  source      TEXT NOT NULL,                   -- datasheet | app_note | experience_card | history_report
  locator     TEXT NOT NULL,                   -- 文件 + 页/节
  text_hash   TEXT NOT NULL,
  ingested_at TEXT NOT NULL
);

CREATE TABLE freezes (
  kind      TEXT PRIMARY KEY CHECK (kind IN ('model_package','safety','task_set')),
                                                -- 恰好三个取值，不扩张
  hash      TEXT NOT NULL,
  detail    TEXT,
  frozen_at TEXT NOT NULL
);

CREATE TABLE baselines (                       -- 冻结的前值基线（交付物5）
  gate_key           TEXT PRIMARY KEY,         -- H(model_package_hash, constraints_hash,
                                                 --   metrics_hash, scenario_set_hash)
  task_id            TEXT NOT NULL REFERENCES tasks(task_id),  -- 产生该基线的 baseline_gate task
  passed             INTEGER NOT NULL,
  frozen_result_hash TEXT,                     -- passed=1 时非空
  diagnosis_ref      TEXT,                     -- passed=0 时非空，指向排查结论文件
  frozen_at          TEXT NOT NULL
);

CREATE TABLE rejections (                      -- 校验期被拒候选：无 simulation_key 与 scenario_id
  rejection_id   TEXT PRIMARY KEY,
  task_id        TEXT NOT NULL REFERENCES tasks(task_id),
  round_index    INTEGER NOT NULL,
  llm_call_id    TEXT REFERENCES llm_calls(llm_call_id),
  raw_parameters TEXT NOT NULL,                -- 原始取值的规范化 JSON
  reason         TEXT NOT NULL,                -- Literal 枚举，取值同 candidate_rejected 的校验类 cause
  created_at     TEXT NOT NULL
);
