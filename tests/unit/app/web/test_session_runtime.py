from __future__ import annotations

import unittest
import asyncio
import os
from pathlib import Path
import threading
import time
from unittest import mock

from app.services.path_policy import PathPolicy
from app.web.session_runtime import (
    WebSessionCapacityError,
    WebSessionContext,
    WebSessionRegistry,
    normalize_directory,
)

class _FakeFrontendStateService:
    def __init__(self):
        self.profile_providers = []

    def set_tool_execution_profile_provider(self, provider):
        self.profile_providers.append(provider)


class _FakeController:
    def __init__(self):
        self.current_save_dir = str(Path.cwd())
        self.shutdown_calls = 0
        self.shutdown_event = threading.Event()
        self.frontend_state_service = _FakeFrontendStateService()

    def shutdown(self):
        self.shutdown_calls += 1
        self.shutdown_event.set()


class _FakeDirectoryCapability:
    def __init__(self, root: Path, identity: tuple[int, int], close_error=None):
        self.root = root
        self.identity = identity
        self.close_error = close_error
        self.close_calls = 0
        self.closed = False

    def snapshot_bound_identity(self):
        if self.closed:
            raise OSError("capability is closed")
        return self.root, self.identity

    def assert_bound(self):
        if self.closed:
            raise OSError("capability is closed")

    def close(self):
        self.close_calls += 1
        if self.closed:
            return
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

class WebSessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]

        def _monotonic():
            return self.clock[0]

        self.registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=lambda _loop, _send: _FakeController(),
            workflow_factory=lambda controller, _send: object(),
            max_contexts=3,
            idle_ttl_seconds=10.0,
            pinned_session_ids={"__default__"},
            monotonic=_monotonic,
        )

    def test_prune_drops_idle_sessions_but_keeps_pinned_default_context(self):
        default_context = self.registry.get_or_create("__default__")
        stale_context = self.registry.get_or_create("session-a")

        self.clock[0] += 11.0
        self.registry.prune()

        self.assertIn("__default__", self.registry._contexts)
        self.assertNotIn("session-a", self.registry._contexts)
        self.assertEqual(default_context.controller.shutdown_calls, 0)
        self.assertTrue(stale_context.controller.shutdown_event.wait(timeout=1))
        self.assertEqual(stale_context.controller.shutdown_calls, 1)

    def test_registry_evicts_least_recently_used_context_when_capacity_exceeded(self):
        self.registry.get_or_create("__default__")
        first = self.registry.get_or_create("session-a")
        self.clock[0] += 1.0
        second = self.registry.get_or_create("session-b")
        self.clock[0] += 1.0
        self.registry.get_or_create("session-c")

        self.assertNotIn("session-a", self.registry._contexts)
        self.assertIn("session-b", self.registry._contexts)
        self.assertIn("session-c", self.registry._contexts)
        self.assertTrue(first.controller.shutdown_event.wait(timeout=1))
        self.assertEqual(first.controller.shutdown_calls, 1)
        self.assertEqual(second.controller.shutdown_calls, 0)

    def test_dispose_context_returns_without_waiting_for_slow_shutdown(self):
        context = self.registry.get_or_create("slow-session")
        release_shutdown = threading.Event()
        entered_shutdown = threading.Event()

        def slow_shutdown():
            entered_shutdown.set()
            release_shutdown.wait(timeout=1)

        context.controller.shutdown = slow_shutdown

        start = time.perf_counter()
        self.registry._dispose_context("slow-session")

        self.assertNotIn("slow-session", self.registry._contexts)
        self.assertTrue(entered_shutdown.wait(timeout=1))
        self.assertLess(time.perf_counter() - start, 0.2)
        release_shutdown.set()

    def test_disposal_concurrency_and_queue_are_bounded(self):
        release_shutdown = threading.Event()
        workers_started = threading.Event()
        state_lock = threading.Lock()
        active_shutdowns = 0
        max_active_shutdowns = 0
        started_shutdowns = 0
        completed_shutdowns = 0

        class _BlockingController(_FakeController):
            def shutdown(inner_self):
                nonlocal active_shutdowns
                nonlocal max_active_shutdowns
                nonlocal started_shutdowns
                nonlocal completed_shutdowns
                with state_lock:
                    active_shutdowns += 1
                    started_shutdowns += 1
                    max_active_shutdowns = max(max_active_shutdowns, active_shutdowns)
                    if started_shutdowns >= 2:
                        workers_started.set()
                release_shutdown.wait(timeout=2)
                time.sleep(0.02)
                with state_lock:
                    active_shutdowns -= 1
                    completed_shutdowns += 1
                inner_self.shutdown_calls += 1
                inner_self.shutdown_event.set()

        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=lambda _loop, _send: _BlockingController(),
            workflow_factory=lambda controller, _send: object(),
            max_contexts=20,
            idle_ttl_seconds=10.0,
            monotonic=lambda: 100.0,
        )
        contexts = [registry.get_or_create(f"session-{index}") for index in range(11)]

        for context in contexts[:10]:
            registry._dispose_context(context.session_id)

        self.assertTrue(workers_started.wait(timeout=1))
        start = time.perf_counter()
        extra_future = registry._dispose_context(contexts[-1].session_id)
        try:
            self.assertIsNone(extra_future)
            self.assertLess(time.perf_counter() - start, 0.1)
            self.assertIn(contexts[-1].session_id, registry._contexts)
            with state_lock:
                self.assertLessEqual(max_active_shutdowns, 2)
        finally:
            release_shutdown.set()

        for context in contexts[:10]:
            self.assertTrue(context.controller.shutdown_event.wait(timeout=2))
        retry_future = registry._dispose_context(contexts[-1].session_id)
        self.assertIsNotNone(retry_future)
        self.assertTrue(contexts[-1].controller.shutdown_event.wait(timeout=2))
        registry.shutdown_all(wait=True, timeout=2.0)
        self.assertEqual(completed_shutdowns, len(contexts))

    def test_saturated_disposal_capacity_never_blocks_the_event_loop(self):
        release_shutdown = threading.Event()
        entered_shutdown = threading.Event()
        constructed_controllers = 0

        class _BlockingController(_FakeController):
            def shutdown(inner_self):
                entered_shutdown.set()
                release_shutdown.wait(timeout=0.5)
                inner_self.shutdown_calls += 1
                inner_self.shutdown_event.set()

        def controller_factory(_loop, _send):
            nonlocal constructed_controllers
            constructed_controllers += 1
            return _BlockingController()

        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=controller_factory,
            workflow_factory=lambda controller, _send: object(),
            max_contexts=2,
            idle_ttl_seconds=10.0,
            monotonic=lambda: 100.0,
            disposal_workers=1,
            disposal_queue_capacity=0,
        )
        first = registry.get_or_create("session-a")
        second = registry.get_or_create("session-b")
        registry._dispose_context(first.session_id)
        self.assertTrue(entered_shutdown.wait(timeout=1))
        third = registry.get_or_create("session-c")

        async def run_case():
            heartbeat = asyncio.create_task(asyncio.sleep(0.01))
            start = time.perf_counter()
            with self.assertRaises(WebSessionCapacityError):
                registry.get_or_create("session-d")
            await asyncio.wait_for(heartbeat, timeout=0.1)
            self.assertLess(time.perf_counter() - start, 0.1)

        try:
            asyncio.run(run_case())
            self.assertIs(registry.get_or_create(second.session_id), second)
            self.assertEqual(constructed_controllers, 3)
            self.assertEqual(set(registry._contexts), {second.session_id, third.session_id})
        finally:
            release_shutdown.set()
            registry.shutdown_all(wait=True, timeout=2.0)

    def test_concurrent_session_admission_cannot_exceed_capacity(self):
        release_shutdown = threading.Event()
        entered_shutdown = threading.Event()

        class _BlockingController(_FakeController):
            def shutdown(inner_self):
                entered_shutdown.set()
                release_shutdown.wait(timeout=1)
                inner_self.shutdown_calls += 1
                inner_self.shutdown_event.set()

        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=lambda _loop, _send: _BlockingController(),
            workflow_factory=lambda controller, _send: object(),
            max_contexts=1,
            idle_ttl_seconds=10.0,
            monotonic=lambda: 100.0,
            disposal_workers=1,
            disposal_queue_capacity=0,
        )
        original = registry.get_or_create("original")
        barrier = threading.Barrier(3)
        admitted: list[str] = []
        rejected: list[str] = []

        def admit(session_id: str) -> None:
            barrier.wait()
            try:
                registry.get_or_create(session_id)
            except WebSessionCapacityError:
                rejected.append(session_id)
            else:
                admitted.append(session_id)

        threads = [
            threading.Thread(target=admit, args=(session_id,), daemon=True)
            for session_id in ("session-a", "session-b")
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=1)

        try:
            self.assertTrue(entered_shutdown.wait(timeout=1))
            self.assertEqual(len(admitted), 1)
            self.assertEqual(len(rejected), 1)
            self.assertEqual(len(registry._contexts), 1)
            self.assertNotIn(original.session_id, registry._contexts)
        finally:
            release_shutdown.set()
            registry.shutdown_all(wait=True, timeout=2.0)

    def test_capacity_rejection_does_not_construct_or_replace_a_protected_context(self):
        constructed_controllers = 0

        def controller_factory(_loop, _send):
            nonlocal constructed_controllers
            constructed_controllers += 1
            return _FakeController()

        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=controller_factory,
            workflow_factory=lambda controller, _send: object(),
            max_contexts=1,
            pinned_session_ids={"pinned"},
        )
        pinned = registry.get_or_create("pinned")

        with self.assertRaises(WebSessionCapacityError):
            registry.get_or_create("new-session")

        self.assertEqual(constructed_controllers, 1)
        self.assertIs(registry.get_or_create("pinned"), pinned)
        self.assertEqual(registry._contexts, {"pinned": pinned})
        registry.shutdown_all(wait=True, timeout=1.0)

    def test_submit_failure_restores_eviction_candidate_and_releases_capacity(self):
        controllers: list[_FakeController] = []

        def controller_factory(_loop, _send):
            controller = _FakeController()
            controllers.append(controller)
            return controller

        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=controller_factory,
            workflow_factory=lambda controller, _send: object(),
            max_contexts=1,
            disposal_workers=1,
            disposal_queue_capacity=0,
        )
        original = registry.get_or_create("original")

        with mock.patch.object(
            registry._disposal_executor,
            "submit",
            side_effect=RuntimeError("executor unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "executor unavailable"):
                registry.get_or_create("failed-admission")

        self.assertEqual(registry._contexts, {"original": original})
        self.assertEqual(original.controller.shutdown_calls, 0)
        self.assertEqual(controllers[1].shutdown_calls, 1)

        replacement = registry.get_or_create("replacement")
        self.assertIs(registry._contexts["replacement"], replacement)
        self.assertTrue(original.controller.shutdown_event.wait(timeout=1))
        registry.shutdown_all(wait=True, timeout=1.0)

    def test_prune_keeps_stale_context_until_disposal_capacity_recovers(self):
        clock = [100.0]
        release_shutdown = threading.Event()

        class _BlockingController(_FakeController):
            def shutdown(inner_self):
                release_shutdown.wait(timeout=1)
                inner_self.shutdown_calls += 1
                inner_self.shutdown_event.set()

        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=lambda _loop, _send: _BlockingController(),
            workflow_factory=lambda controller, _send: object(),
            max_contexts=2,
            idle_ttl_seconds=1.0,
            monotonic=lambda: clock[0],
            disposal_workers=1,
            disposal_queue_capacity=0,
        )
        first = registry.get_or_create("session-a")
        stale = registry.get_or_create("session-b")
        registry._dispose_context(first.session_id)
        clock[0] += 2.0

        registry.prune()
        self.assertIn(stale.session_id, registry._contexts)

        release_shutdown.set()
        self.assertTrue(first.controller.shutdown_event.wait(timeout=1))
        deadline = time.monotonic() + 1.0
        while stale.session_id in registry._contexts and time.monotonic() < deadline:
            registry.prune()
            time.sleep(0.01)
        self.assertNotIn(stale.session_id, registry._contexts)
        self.assertTrue(stale.controller.shutdown_event.wait(timeout=1))
        registry.shutdown_all(wait=True, timeout=2.0)

    def test_prune_rechecks_candidate_after_websocket_lease_is_acquired(self):
        clock = [100.0]
        selected_for_prune = threading.Event()
        continue_prune = threading.Event()
        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=lambda _loop, _send: _FakeController(),
            workflow_factory=lambda controller, _send: object(),
            max_contexts=2,
            idle_ttl_seconds=1.0,
            monotonic=lambda: clock[0],
            disposal_workers=1,
            disposal_queue_capacity=0,
        )
        context = registry.get_or_create("session-a")
        clock[0] += 2.0
        original_dispose = registry._dispose_context

        def delayed_dispose(session_id, **kwargs):
            selected_for_prune.set()
            continue_prune.wait(timeout=1)
            return original_dispose(session_id, **kwargs)

        registry._dispose_context = delayed_dispose
        prune_thread = threading.Thread(target=registry.prune, daemon=True)
        prune_thread.start()
        self.assertTrue(selected_for_prune.wait(timeout=1))

        leased = registry.acquire_websocket_context(
            context.session_id,
            context.session_token,
        )
        continue_prune.set()
        prune_thread.join(timeout=1)

        self.assertFalse(prune_thread.is_alive())
        self.assertIs(leased, context)
        self.assertIs(registry._contexts[context.session_id], context)
        self.assertEqual(context.controller.shutdown_calls, 0)
        context.mark_websocket_disconnected()
        registry._dispose_context = original_dispose
        registry.shutdown_all(wait=True, timeout=1.0)

    def test_prune_rechecks_candidate_after_http_lease_is_acquired(self):
        clock = [100.0]
        selected_for_prune = threading.Event()
        continue_prune = threading.Event()
        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=lambda _loop, _send: _FakeController(),
            workflow_factory=lambda controller, _send: object(),
            max_contexts=2,
            idle_ttl_seconds=1.0,
            monotonic=lambda: clock[0],
            disposal_workers=1,
            disposal_queue_capacity=0,
        )
        context = registry.get_or_create("session-a")
        clock[0] += 2.0
        original_dispose = registry._dispose_context

        def delayed_dispose(session_id, **kwargs):
            selected_for_prune.set()
            continue_prune.wait(timeout=1)
            return original_dispose(session_id, **kwargs)

        registry._dispose_context = delayed_dispose
        prune_thread = threading.Thread(target=registry.prune, daemon=True)
        prune_thread.start()
        self.assertTrue(selected_for_prune.wait(timeout=1))

        leased = registry.acquire_http_context(context.session_id)
        continue_prune.set()
        prune_thread.join(timeout=1)

        self.assertFalse(prune_thread.is_alive())
        self.assertIs(leased, context)
        self.assertIs(registry._contexts[context.session_id], context)
        self.assertEqual(context.controller.shutdown_calls, 0)
        registry.release_http_context(context)
        registry._dispose_context = original_dispose
        registry.shutdown_all(wait=True, timeout=1.0)

    def test_http_lease_blocks_ttl_prune_and_completion_refreshes_idle_time(self):
        context = self.registry.acquire_http_context("session-http")
        self.clock[0] += 11.0

        self.registry.prune()

        self.assertIn(context.session_id, self.registry._contexts)
        self.assertTrue(context.has_active_http_request())
        self.registry.release_http_context(context)
        self.assertFalse(context.has_active_http_request())
        self.assertEqual(context.last_access_at, 111.0)

        self.clock[0] += 9.0
        self.registry.prune()
        self.assertIn(context.session_id, self.registry._contexts)

        self.clock[0] += 2.0
        self.registry.prune()
        self.assertNotIn(context.session_id, self.registry._contexts)
        self.assertTrue(context.controller.shutdown_event.wait(timeout=1))

    def test_capacity_does_not_evict_an_active_http_context(self):
        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=lambda _loop, _send: _FakeController(),
            workflow_factory=lambda controller, _send: object(),
            max_contexts=1,
            disposal_workers=1,
            disposal_queue_capacity=0,
        )
        active = registry.acquire_http_context("active-http")

        with self.assertRaises(WebSessionCapacityError):
            registry.get_or_create("replacement")

        self.assertIs(registry._contexts[active.session_id], active)
        self.assertEqual(active.controller.shutdown_calls, 0)
        registry.release_http_context(active)

        replacement = registry.get_or_create("replacement")
        self.assertIs(registry._contexts[replacement.session_id], replacement)
        self.assertTrue(active.controller.shutdown_event.wait(timeout=1))
        registry.shutdown_all(wait=True, timeout=1.0)

    def test_explicit_disposal_does_not_detach_an_active_http_context(self):
        context = self.registry.acquire_http_context("active-http")

        self.assertIsNone(self.registry._dispose_context(context.session_id))
        self.assertIs(self.registry._contexts[context.session_id], context)

        self.registry.release_http_context(context)
        future = self.registry._dispose_context(context.session_id)
        self.assertIsNotNone(future)
        self.assertTrue(context.controller.shutdown_event.wait(timeout=1))

    def test_http_release_rejects_underflow_without_refreshing_idle_time(self):
        context = self.registry.acquire_http_context("underflow")
        self.clock[0] += 2.0
        self.registry.release_http_context(context)
        released_at = context.last_access_at
        self.clock[0] += 3.0

        with self.assertRaisesRegex(RuntimeError, "HTTP request reference underflow"):
            self.registry.release_http_context(context)

        self.assertEqual(context.last_access_at, released_at)

    def test_concurrent_http_leases_require_balanced_release_before_disposal(self):
        barrier = threading.Barrier(3)
        acquired: list[WebSessionContext] = []

        def acquire() -> None:
            barrier.wait()
            acquired.append(self.registry.acquire_http_context("shared-http"))

        threads = [threading.Thread(target=acquire, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=1)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(acquired), 2)
        self.assertIs(acquired[0], acquired[1])
        self.registry.release_http_context(acquired[0])
        self.assertTrue(acquired[0].has_active_http_request())
        self.assertIsNone(self.registry._dispose_context("shared-http"))

        self.registry.release_http_context(acquired[1])
        self.assertFalse(acquired[0].has_active_http_request())
        self.assertIsNotNone(self.registry._dispose_context("shared-http"))
        self.assertTrue(acquired[0].controller.shutdown_event.wait(timeout=1))

    def test_hostile_completion_clock_does_not_leave_a_ghost_http_pin(self):
        class ClockAbort(BaseException):
            pass

        context = self.registry.acquire_http_context("hostile-clock")
        primary = ClockAbort("clock interrupted")

        def aborting_clock():
            raise primary

        context._monotonic = aborting_clock

        with self.assertRaises(ClockAbort) as raised:
            self.registry.release_http_context(context)

        self.assertIs(raised.exception, primary)
        self.assertFalse(context.has_active_http_request())
        self.assertIsNotNone(self.registry._dispose_context(context.session_id))
        self.assertTrue(context.controller.shutdown_event.wait(timeout=1))

    def test_shutdown_all_forces_active_http_context_into_terminal_disposal(self):
        context = self.registry.acquire_http_context("active-http")

        self.registry.shutdown_all(wait=True, timeout=1.0)

        self.assertEqual(self.registry._contexts, {})
        self.assertTrue(context.controller.shutdown_event.is_set())
        self.assertEqual(context.controller.shutdown_calls, 1)
        self.registry.release_http_context(context)

    def test_shutdown_all_retains_and_drains_terminal_backlog_after_timeout(self):
        release_shutdown = threading.Event()
        controllers: list[_FakeController] = []

        class _BlockingController(_FakeController):
            def shutdown(inner_self):
                release_shutdown.wait(timeout=1)
                inner_self.shutdown_calls += 1
                inner_self.shutdown_event.set()

        def controller_factory(_loop, _send):
            controller = _BlockingController()
            controllers.append(controller)
            return controller

        registry = WebSessionRegistry(
            send_factory=lambda _session_id: lambda _event_type, _data=None: None,
            controller_factory=controller_factory,
            workflow_factory=lambda controller, _send: object(),
            max_contexts=3,
            disposal_workers=1,
            disposal_queue_capacity=0,
        )
        for index in range(3):
            registry.get_or_create(f"session-{index}")

        start = time.perf_counter()
        registry.shutdown_all(wait=True, timeout=0.05)

        self.assertLess(time.perf_counter() - start, 0.2)
        self.assertEqual(registry._contexts, {})
        self.assertTrue(registry._terminal_disposal_backlog)

        release_shutdown.set()
        registry.shutdown_all(wait=True, timeout=2.0)
        for controller in controllers:
            self.assertTrue(controller.shutdown_event.wait(timeout=1))
            self.assertEqual(controller.shutdown_calls, 1)

    def test_prune_keeps_context_with_active_websocket(self):
        context = self.registry.get_or_create("session-active")
        context.mark_websocket_connected()
        self.clock[0] += 11.0

        self.registry.prune()

        self.assertIn("session-active", self.registry._contexts)
        self.assertEqual(context.controller.shutdown_calls, 0)

    def test_context_tracks_background_tasks_until_done(self):
        async def run_case():
            context = self.registry.get_or_create("task-session")
            task = asyncio.create_task(asyncio.sleep(0))

            tracked = context.track_background_task(task)

            self.assertIs(tracked, task)
            self.assertIn(task, context.background_tasks)
            await task
            await asyncio.sleep(0)
            self.assertNotIn(task, context.background_tasks)

        asyncio.run(run_case())

    def test_context_binds_one_dynamic_public_tool_profile_provider(self):
        context = self.registry.get_or_create("tool-session")
        service = context.controller.frontend_state_service

        self.assertEqual(len(service.profile_providers), 1)
        provider = service.profile_providers[0]
        initial = provider()
        self.assertEqual(initial.host_surface, "public_web")
        self.assertEqual(initial.owner_id, "web:tool-session")
        self.assertFalse(initial.allow_tool_execution)
        self.assertEqual(initial.tool_permissions, frozenset())
        self.assertEqual(initial.approved_roots, frozenset())

        context.approve_directory(str(Path.cwd()))
        refreshed = provider()

        self.assertEqual(refreshed.owner_id, initial.owner_id)
        self.assertEqual(refreshed.approved_roots, frozenset())
        self.assertEqual(len(service.profile_providers), 1)

    def test_missing_default_directory_is_created_before_its_grant_is_captured(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            default_directory = Path(temp_dir) / "downloads" / "session-default"
            controller = _FakeController()
            controller.current_save_dir = str(default_directory)

            context = WebSessionContext(
                "missing-default-directory",
                send_factory=lambda _session_id: lambda _event_type, _data=None: None,
                controller_factory=lambda _loop, _send: controller,
                workflow_factory=lambda _controller, _send: object(),
            )

            self.assertTrue(default_directory.is_dir())
            self.assertEqual(
                context.approved_roots_snapshot(),
                (normalize_directory(str(default_directory)),),
            )
            with PathPolicy().lease_approved_root_grants(
                context.approved_root_grants_snapshot()
            ) as lease:
                lease.assert_bound()

    def test_explicit_approval_requires_an_existing_regular_directory(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            context = self.registry.get_or_create("explicit-directory")
            missing_directory = Path(temp_dir) / "must-not-be-created-by-approval"
            roots_before = context.approved_roots_snapshot()
            grants_before = context.approved_root_grants_snapshot()

            with self.assertRaises(FileNotFoundError):
                context.approve_directory(str(missing_directory))

            self.assertFalse(missing_directory.exists())
            self.assertEqual(context.approved_roots_snapshot(), roots_before)
            self.assertEqual(context.approved_root_grants_snapshot(), grants_before)

    def test_directory_reapproval_replaces_only_the_explicitly_refreshed_generation(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            workspace = Path(temp_dir)
            approved = workspace / "approved"
            approved.mkdir()
            controller = _FakeController()
            controller.current_save_dir = str(approved)
            context = WebSessionContext(
                "grant-session",
                send_factory=lambda _session_id: lambda _event_type, _data=None: None,
                controller_factory=lambda _loop, _send: controller,
                workflow_factory=lambda _controller, _send: object(),
            )

            first_grants = context.approved_root_grants_snapshot()
            first_roots = context.approved_roots_snapshot()
            self.assertEqual(first_roots, (normalize_directory(str(approved)),))

            previous_generation = workspace / "previous-generation"
            if os.name == "nt":
                with self.assertRaises(PermissionError):
                    approved.rename(previous_generation)
                self.assertEqual(context.approved_root_grants_snapshot(), first_grants)
                with context.lease_approved_root_grants_snapshot() as leased:
                    self.assertEqual(leased, first_grants)
                context.close_approved_root_authorities()
                return
            approved.rename(previous_generation)
            approved.mkdir()

            self.assertEqual(context.approved_root_grants_snapshot(), first_grants)
            self.assertEqual(context.approved_roots_snapshot(), ())
            self.assertFalse(context.is_directory_allowed(str(approved)))
            with self.assertRaises(PermissionError):
                with PathPolicy().lease_approved_root_grants(first_grants):
                    pass

            context.approve_directory(str(approved))
            refreshed_grants = context.approved_root_grants_snapshot()

            self.assertEqual(context.approved_roots_snapshot(), first_roots)
            self.assertTrue(context.is_directory_allowed(str(approved)))
            self.assertEqual(refreshed_grants[0][0], first_grants[0][0])
            self.assertNotEqual(refreshed_grants[0][1], first_grants[0][1])
            with PathPolicy().lease_approved_root_grants(refreshed_grants) as lease:
                lease.assert_bound()

    def test_reapproval_retires_but_does_not_close_a_leased_generation(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            approved = Path(temp_dir)
            first = _FakeDirectoryCapability(approved, (11, 101))
            replacement = _FakeDirectoryCapability(approved, (11, 202))
            controller = _FakeController()
            controller.current_save_dir = str(approved)
            with mock.patch(
                "app.web.session_runtime.open_filesystem_directory_capability",
                side_effect=(first, replacement),
            ):
                context = WebSessionContext(
                    "lease-reapproval",
                    send_factory=lambda _session_id: lambda *_args: None,
                    controller_factory=lambda _loop, _send: controller,
                    workflow_factory=lambda _controller, _send: object(),
                )
                with context.lease_approved_root_grants_snapshot() as leased:
                    self.assertEqual(
                        leased,
                        (
                            (
                                os.path.normcase(os.path.abspath(str(approved))),
                                (11, 101),
                            ),
                        ),
                    )
                    context.approve_directory(str(approved))
                    self.assertEqual(first.close_calls, 0)
                    self.assertEqual(
                        context.approved_root_grants_snapshot(),
                        (
                            (
                                os.path.normcase(os.path.abspath(str(approved))),
                                (11, 202),
                            ),
                        ),
                    )

                self.assertEqual(first.close_calls, 1)
                self.assertEqual(replacement.close_calls, 0)
                context.close_approved_root_authorities()
                self.assertEqual(replacement.close_calls, 1)

    def test_disposal_closes_authorities_even_when_controller_raises_control_flow(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            approved = Path(temp_dir)
            capability = _FakeDirectoryCapability(approved, (12, 303))
            control_flow = KeyboardInterrupt("controller shutdown interrupted")
            controller = _FakeController()
            controller.current_save_dir = str(approved)
            controller.shutdown = mock.Mock(side_effect=control_flow)
            registry = WebSessionRegistry(
                send_factory=lambda _session_id: lambda *_args: None,
                controller_factory=lambda _loop, _send: controller,
                workflow_factory=lambda _controller, _send: object(),
            )
            with mock.patch(
                "app.web.session_runtime.open_filesystem_directory_capability",
                return_value=capability,
            ):
                registry.get_or_create("dispose-control-flow")
                future = registry._dispose_context("dispose-control-flow")

            self.assertIsNotNone(future)
            with self.assertRaises(KeyboardInterrupt) as raised:
                future.result(timeout=1)
            self.assertIs(raised.exception, control_flow)
            self.assertEqual(capability.close_calls, 1)

    def test_disposal_without_controller_shutdown_still_closes_authorities(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            approved = Path(temp_dir)
            capability = _FakeDirectoryCapability(approved, (12, 304))
            controller = type(
                "ControllerWithoutShutdown",
                (),
                {"current_save_dir": str(approved)},
            )()
            registry = WebSessionRegistry(
                send_factory=lambda _session_id: lambda *_args: None,
                controller_factory=lambda _loop, _send: controller,
                workflow_factory=lambda _controller, _send: object(),
            )
            with mock.patch(
                "app.web.session_runtime.open_filesystem_directory_capability",
                return_value=capability,
            ):
                registry.get_or_create("dispose-without-controller-shutdown")
                future = registry._dispose_context(
                    "dispose-without-controller-shutdown"
                )

            self.assertIsNotNone(future)
            future.result(timeout=1)
            self.assertEqual(capability.close_calls, 1)

    def test_authority_close_prefers_first_control_flow_after_closing_every_root(self):
        from tempfile import TemporaryDirectory

        class CloseControlFlow(BaseException):
            pass

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            workspace = Path(temp_dir)
            first_root = workspace / "first"
            second_root = workspace / "second"
            first_root.mkdir()
            second_root.mkdir()
            ordinary_error = RuntimeError("first close failed")
            control_flow = CloseControlFlow("second close interrupted")
            first = _FakeDirectoryCapability(
                first_root, (13, 404), close_error=ordinary_error
            )
            second = _FakeDirectoryCapability(
                second_root, (13, 505), close_error=control_flow
            )
            controller = _FakeController()
            controller.current_save_dir = str(first_root)
            with mock.patch(
                "app.web.session_runtime.open_filesystem_directory_capability",
                side_effect=(first, second),
            ):
                context = WebSessionContext(
                    "close-matrix",
                    send_factory=lambda _session_id: lambda *_args: None,
                    controller_factory=lambda _loop, _send: controller,
                    workflow_factory=lambda _controller, _send: object(),
                )
                context.approve_directory(str(second_root))
                with self.assertRaises(CloseControlFlow) as raised:
                    context.close_approved_root_authorities()

            self.assertIs(raised.exception, control_flow)
            self.assertEqual(first.close_calls, 1)
            self.assertEqual(second.close_calls, 1)

    def test_constructor_failure_closes_the_default_directory_authority(self):
        from tempfile import TemporaryDirectory

        class BindControlFlow(BaseException):
            pass

        with TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            approved = Path(temp_dir)
            capability = _FakeDirectoryCapability(approved, (14, 606))
            primary = BindControlFlow("profile binding interrupted")
            controller = _FakeController()
            controller.current_save_dir = str(approved)
            controller.frontend_state_service.set_tool_execution_profile_provider = mock.Mock(
                side_effect=primary
            )
            with mock.patch(
                "app.web.session_runtime.open_filesystem_directory_capability",
                return_value=capability,
            ):
                with self.assertRaises(BindControlFlow) as raised:
                    WebSessionContext(
                        "constructor-failure",
                        send_factory=lambda _session_id: lambda *_args: None,
                        controller_factory=lambda _loop, _send: controller,
                        workflow_factory=lambda _controller, _send: object(),
                    )

            self.assertIs(raised.exception, primary)
            self.assertEqual(capability.close_calls, 1)

    def test_shutdown_all_disposes_every_context_including_pinned_sessions(self):
        default_context = self.registry.get_or_create("__default__")
        other_context = self.registry.get_or_create("session-a")

        self.registry.shutdown_all(wait=True, timeout=1.0)

        self.assertEqual(self.registry._contexts, {})
        self.assertTrue(default_context.controller.shutdown_event.is_set())
        self.assertTrue(other_context.controller.shutdown_event.is_set())
        self.assertEqual(default_context.controller.shutdown_calls, 1)
        self.assertEqual(other_context.controller.shutdown_calls, 1)

if __name__ == "__main__":
    unittest.main()
