# QwenPaw Relative: Portfolio and Contribution Guide

This repository presents work by **Xinyi Jiang (蒋心逸)** during an internship: a progression from data cleaning and multi-user isolation to a social-media trend workflow and an investment-research workbench for Xuzhou. Development used AI coding assistance. Xinyi led requirements clarification and iteration, participated in solution and interface design, and verified and deployed the resulting work. The repository contains upstream code and collaboratively produced changes; its entire codebase should not be described as independently written from scratch.

## Foundation and contributions

| Capability | QwenPaw foundation | Project extension |
| --- | --- | --- |
| Agent platform | Agent runtime, model integration, chat, workspaces, plugins and Skills | Business-specific tools and interfaces built on the existing platform |
| Access control | Existing authentication, workspace and server routing | Login/registration and user-management enhancements; a `Principal` model, ownership checks and cross-account isolation tests |
| Data preparation | Existing tool and file infrastructure | CSV deduplication, missing-value imputation, Z-score outlier treatment and Excel export prototype |
| Xiaohongshu trends | Plugins, scheduling, workspaces and chat | Third-party trend collection, validation and snapshots, personalized reports and Excel; email after human confirmation |
| Xuzhou research | Plugin loading, chat, channels and scheduling | Shared lead and evidence ledger, human review, bounded source learning, relationship views and a separate business portal |
| DingTalk | Existing channel integration and messaging | A digest for a configured single conversation, with a bounded retry; channel credentials and recipient binding are configured separately |

“Learning” means using recorded human decisions and outcomes to adjust source-reading priorities or propose changes to fixed source recipes under configured controls. It does not retrain the language model, invent evidence, or contact organizations.

## Explore the code

- [`csv_cleaner.py`](../csv_cleaner.py): data-cleaning prototype. The initial addition is visible in [commit d2f8e7b3](https://github.com/CatHissesJustWannaPlayWithU/QwenPaw_Relative/commit/d2f8e7b3ea720bb68bcbd494b7c1de574f789422).
- [`src/qwenpaw/app/principal.py`](../src/qwenpaw/app/principal.py), [`auth.py`](../src/qwenpaw/app/auth.py), and [`routers/`](../src/qwenpaw/app/routers/): authentication and ownership extensions. See [user management](https://github.com/CatHissesJustWannaPlayWithU/QwenPaw_Relative/commit/31a28735b36d0876d320451239dcacb12703dcc5) and [resource isolation](https://github.com/CatHissesJustWannaPlayWithU/QwenPaw_Relative/commit/4eda63f5).
- [`plugins/tool/xhs-hotspot/`](../plugins/tool/xhs-hotspot/): validation, traceable snapshots, reports and scheduling for a third-party Xiaohongshu trend source.
- [`plugins/tool/xuzhou-investment-ledger/`](../plugins/tool/xuzhou-investment-ledger/): candidate discovery, evidence capture, review, shared access, reporting, source governance and audit. The plugin version is **0.43.2**.
- [`xuzhou-business-portal/`](../xuzhou-business-portal/): a separate interface for nontechnical staff, reusing QwenPaw's authenticated APIs, accounts and ledger rather than introducing a second backend.
- [`dingtalk_daily_digest.py`](../plugins/tool/xuzhou-investment-ledger/dingtalk_daily_digest.py) and [`crons/executor.py`](../src/qwenpaw/app/crons/executor.py): project-specific digest and controlled scheduled-delivery integration.

## Workflow and boundaries

Public sources or authorized human submissions become reviewable materials. The server records and checks materials under configured rules; business users assess their value, select an owner and decide whether to follow up. A webpage snapshot is not automatically a verified fact, and entry into an internal follow-up pool is not an investment commitment, signed agreement or completed project. The system does not automatically contact companies.

The repository excludes production credentials, recipient bindings, operational ledgers and captured business records. A fresh installation requires its own accounts, model, source and channel configuration; external services may change or become unavailable.

## Build and verify

Follow the [original QwenPaw documentation](../README.md#original-qwenpaw-documentation) to prepare the backend. This repository is based on the QwenPaw `2.0.1b1` code line; the Xuzhou manifest declares `>=2.0.0, <2.1.0`. Compatibility with other versions has not been exhaustively verified.

The Xuzhou plugin's generated frontend is not committed. Before installing it via QwenPaw's plugin mechanism, build it from source:

```bash
cd plugins/tool/xuzhou-investment-ledger/frontend
npm ci
npm run typecheck
npm run build
```

Then return to the repository root and use the configured QwenPaw environment to install the plugin:

```bash
qwenpaw plugin install ./plugins/tool/xuzhou-investment-ledger
```

The business portal is served by the same application at `/business/` and requires appropriate account and team permissions. See the [business portal guide](../xuzhou-business-portal/README.md) and [Xiaohongshu plugin guide](../plugins/tool/xhs-hotspot/README.md) for configuration details.

Representative unit tests are in [`tests/unit/plugins/`](../tests/unit/plugins/), [`tests/unit/app/auth/`](../tests/unit/app/auth/), and [`tests/unit/app/crons/`](../tests/unit/app/crons/). In the local portfolio preparation, **184 selected tests passed**, and the Xuzhou frontend passed type checking and production build. These checks cover evidence and review boundaries, shared-ledger permissions, digest content, scheduling and user isolation; they did not test a live DingTalk account or external sources.

## Upstream attribution

The upstream project is [agentscope-ai/QwenPaw](https://github.com/agentscope-ai/QwenPaw). Its history, original documentation and [Apache 2.0 license](../LICENSE) remain in the repository. The [comparison from upstream baseline `f433bbff` to main](https://github.com/CatHissesJustWannaPlayWithU/QwenPaw_Relative/compare/f433bbff...main) shows subsequent project changes, including modifications to upstream files and build artifacts as well as new modules.
