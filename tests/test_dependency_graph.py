import pytest

from ralph import DependencyCycleError, DependencyGraph

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_task(task_id: str, depends_on: list[str] | None = None) -> dict:
    return {"id": task_id, "depends_on": depends_on or []}


# ---------------------------------------------------------------------------
# build_graph
# ---------------------------------------------------------------------------


class TestBuildGraph:
    def test_empty_task_list(self):
        g = DependencyGraph()
        g.build_graph([])
        assert g.topological_sort() == []

    def test_single_task_no_deps(self):
        g = DependencyGraph()
        g.build_graph([make_task("A")])
        assert g.topological_sort() == ["A"]

    def test_linear_chain(self):
        """A → B → C must sort as [A, B, C]."""
        g = DependencyGraph()
        g.build_graph(
            [
                make_task("A"),
                make_task("B", ["A"]),
                make_task("C", ["B"]),
            ]
        )
        order = g.topological_sort()
        assert order.index("A") < order.index("B") < order.index("C")

    def test_diamond_dependency(self):
        """A → B, A → C, B → D, C → D — D must come last."""
        g = DependencyGraph()
        g.build_graph(
            [
                make_task("A"),
                make_task("B", ["A"]),
                make_task("C", ["A"]),
                make_task("D", ["B", "C"]),
            ]
        )
        order = g.topological_sort()
        assert order.index("A") < order.index("B")
        assert order.index("A") < order.index("C")
        assert order.index("B") < order.index("D")
        assert order.index("C") < order.index("D")

    def test_build_graph_is_idempotent(self):
        """Calling build_graph twice resets state."""
        g = DependencyGraph()
        g.build_graph([make_task("A"), make_task("B", ["A"])])
        g.build_graph([make_task("X")])
        assert g.topological_sort() == ["X"]


# ---------------------------------------------------------------------------
# validate_dependencies
# ---------------------------------------------------------------------------


class TestValidateDependencies:
    def test_no_missing_deps(self):
        g = DependencyGraph()
        g.build_graph([make_task("A"), make_task("B", ["A"])])
        assert g.validate_dependencies() == []

    def test_detects_missing_single_dep(self):
        g = DependencyGraph()
        g.build_graph([make_task("B", ["GHOST"])])
        missing = g.validate_dependencies()
        assert "GHOST" in missing

    def test_detects_multiple_missing_deps(self):
        g = DependencyGraph()
        g.build_graph(
            [
                make_task("A", ["X"]),
                make_task("B", ["Y"]),
            ]
        )
        missing = g.validate_dependencies()
        assert "X" in missing
        assert "Y" in missing

    def test_no_duplicates_in_missing(self):
        """Same missing ID referenced by two tasks should appear only once."""
        g = DependencyGraph()
        g.build_graph(
            [
                make_task("A", ["GHOST"]),
                make_task("B", ["GHOST"]),
            ]
        )
        missing = g.validate_dependencies()
        assert missing.count("GHOST") == 1


# ---------------------------------------------------------------------------
# detect_cycles
# ---------------------------------------------------------------------------


class TestDetectCycles:
    def test_no_cycle_returns_false(self):
        g = DependencyGraph()
        g.build_graph([make_task("A"), make_task("B", ["A"])])
        assert g.detect_cycles() is False

    def test_self_loop_returns_true(self):
        g = DependencyGraph()
        g.build_graph([make_task("A", ["A"])])
        assert g.detect_cycles() is True

    def test_two_node_cycle(self):
        g = DependencyGraph()
        g.build_graph([make_task("A", ["B"]), make_task("B", ["A"])])
        assert g.detect_cycles() is True

    def test_three_node_cycle(self):
        g = DependencyGraph()
        g.build_graph(
            [
                make_task("A", ["C"]),
                make_task("B", ["A"]),
                make_task("C", ["B"]),
            ]
        )
        assert g.detect_cycles() is True

    def test_cycle_in_subgraph(self):
        """Acyclic root with a cyclic component elsewhere."""
        g = DependencyGraph()
        g.build_graph(
            [
                make_task("ROOT"),
                make_task("X", ["Y"]),
                make_task("Y", ["X"]),
            ]
        )
        assert g.detect_cycles() is True

    def test_empty_graph_no_cycle(self):
        g = DependencyGraph()
        g.build_graph([])
        assert g.detect_cycles() is False


# ---------------------------------------------------------------------------
# topological_sort
# ---------------------------------------------------------------------------


class TestTopologicalSort:
    def test_all_nodes_present_in_result(self):
        g = DependencyGraph()
        tasks = [make_task("A"), make_task("B", ["A"]), make_task("C", ["A"])]
        g.build_graph(tasks)
        result = g.topological_sort()
        assert set(result) == {"A", "B", "C"}

    def test_dependency_precedes_dependent(self):
        g = DependencyGraph()
        g.build_graph([make_task("T1"), make_task("T2", ["T1"]), make_task("T3", ["T2"])])
        order = g.topological_sort()
        assert order.index("T1") < order.index("T2")
        assert order.index("T2") < order.index("T3")

    def test_parallel_tasks_both_in_result(self):
        """Two independent tasks after a shared root."""
        g = DependencyGraph()
        g.build_graph([make_task("ROOT"), make_task("P1", ["ROOT"]), make_task("P2", ["ROOT"])])
        order = g.topological_sort()
        assert order.index("ROOT") < order.index("P1")
        assert order.index("ROOT") < order.index("P2")


# ---------------------------------------------------------------------------
# DependencyCycleError
# ---------------------------------------------------------------------------


class TestDependencyCycleError:
    def test_raises_on_cycle(self):
        g = DependencyGraph()
        g.build_graph([make_task("A", ["B"]), make_task("B", ["A"])])
        with pytest.raises(DependencyCycleError):
            g.topological_sort()

    def test_error_message_names_cycle_nodes(self):
        g = DependencyGraph()
        g.build_graph([make_task("A", ["B"]), make_task("B", ["A"])])
        with pytest.raises(DependencyCycleError, match=r"A|B"):
            g.topological_sort()

    def test_error_is_ralph_error_subclass(self):
        from ralph import RalphError

        assert issubclass(DependencyCycleError, RalphError)

    def test_three_node_cycle_error_message(self):
        g = DependencyGraph()
        g.build_graph(
            [
                make_task("X", ["Z"]),
                make_task("Y", ["X"]),
                make_task("Z", ["Y"]),
            ]
        )
        with pytest.raises(DependencyCycleError, match=r"X|Y|Z"):
            g.topological_sort()
