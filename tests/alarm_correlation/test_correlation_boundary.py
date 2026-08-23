from apps.alarm_correlation import graph_distance


def test_graph_distance_is_deterministic_and_bounded():
    graph = {1: [2, 4], 2: [3], 4: [5]}
    assert graph_distance(graph, 1, 3, 3) == 2
    assert graph_distance(graph, 1, 5, 1) is None
    assert graph_distance(graph, 4, 4, 0) == 0
