# PRD - TaskDock: minimal task-tracking backend

## Overview
TaskDock is a small HTTP backend for tracking tasks. MVP-1 delivers a working API
(create / list / get / complete) with strict domain rules, built with NestJS using
**hexagonal architecture**: a framework-free domain, use cases that depend only on ports,
and infrastructure adapters (in-memory persistence, HTTP) that plug in at the edges.

## Goals (MVP-1)
- A runnable NestJS application exposing the frozen HTTP contract below.
- Domain rules enforced in the domain layer, not in controllers.
- Persistence via an in-memory adapter (no database in MVP-1).
- Unit tests for domain + use cases (no HTTP), adapter tests, and one end-to-end smoke test.

## Domain rules
- A task has: `id` (uuid string), `title` (string), `done` (boolean), `createdAt` (ISO-8601 UTC string).
- Title is trimmed; it must be non-empty and at most 120 characters after trimming.
  Violations raise `TaskValidationError`.
- Completing an already-completed task raises `TaskAlreadyDoneError`.
- Looking up an unknown id raises `TaskNotFoundError`.

## Frozen contracts (do not change; interfaces are pre-authored where noted)
- `src/domain/errors.ts`: `TaskValidationError`, `TaskNotFoundError`, `TaskAlreadyDoneError`
  (each extends `Error`, with `name` set to the class name).
- `src/domain/task.ts`: class `Task` with readonly `id`, `title`, `done`, `createdAt`;
  `static create(title: string): Task` (post-trim validation, uuid id, done=false,
  createdAt=now ISO); `complete(): Task` (returns a new completed Task; raises
  `TaskAlreadyDoneError` when already done).
- `src/application/ports/task-repository.port.ts` (pre-authored, DO NOT EDIT):
  `TASK_REPOSITORY` injection token + `TaskRepositoryPort` with
  `save(task)`, `findById(id)`, `findAll()` (all return Promises).
- HTTP surface:
  - `POST /tasks` body `{ "title": string }` -> `201` task JSON; invalid title -> `400`
  - `GET /tasks` -> `200` array of tasks (insertion order)
  - `GET /tasks/:id` -> `200` task JSON; unknown -> `404`
  - `POST /tasks/:id/complete` -> `200` task JSON; unknown -> `404`;
    already done -> `409`
  - Task JSON shape: `{ "id": string, "title": string, "done": boolean, "createdAt": string }`
  - Error body shape: `{ "error": string }` where error is the domain error `name`.

## Architecture requirements
- Layering (import direction only downward): `infrastructure` -> `application` -> `domain`.
  - domain: zero framework imports (no `@nestjs/*`), pure TypeScript.
  - application: use cases + ports only; no infrastructure imports.
  - infrastructure: NestJS controllers/providers; adapters implement ports.
- Composition root (`src/app.module.ts`, `src/tasks.module.ts`, `src/main.ts`) wires the
  `TASK_REPOSITORY` token to the in-memory adapter and the controller to the use cases.
- Use cases are plain classes (constructor-injected port; no NestJS decorators).

## Testing requirements
- Jest + ts-jest, pre-configured; run with `npx jest` (unit) and
  `npx jest --config jest-e2e.json` (e2e).
- Domain and use-case tests must run without any HTTP server.
- Every domain rule above and every HTTP status above must be covered by a test.
- End-to-end smoke test boots the Nest app and exercises all four routes.

## Non-goals (MVP-1)
- No database, no auth, no pagination/filtering, no update/delete, no OpenAPI/Swagger,
  no Docker, no CI pipeline.
