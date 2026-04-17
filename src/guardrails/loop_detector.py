"""Loop detection for agent output repetition.

Detects when an agent produces the same output repeatedly within a sliding
window of invocations. This guards against infinite loops where an agent
keeps returning the same (unhelpful) response.

History is tracked per ``(task_id, role)`` pair so that different agent roles
working on the same task do not interfere with each other's loop detection.
"""

from __future__ import annotations

import hashlib
from collections import deque

from errors import GuardrailExceededError
from autologging import get_logger


log = get_logger(__name__)


class LoopDetector:
    """Detects repeated agent output within a sliding window.

    Parameters
    ----------
    window:
        Number of recent invocations to consider per ``(task_id, role)`` pair.
    threshold:
        Minimum number of identical hashes within the window to trigger.
        E.g. window=3, threshold=2 means: if 2 of the last 3 outputs from
        the same role on the same task are identical, raise.
    """

    def __init__(self, window: int = 5, threshold: int = 3) -> None:
        if window < 1:
            raise ValueError(f"window must be >= 1, got {window}")
        if threshold < 1 or threshold > window:
            raise ValueError(
                f"threshold must be between 1 and window ({window}), got {threshold}"
            )
        self._window = window
        self._threshold = threshold
        # Key: (task_id, role) → deque of recent hashes.
        self._history: dict[tuple[str, str], deque[str]] = {}

    def observe(self, task_id: str, role: str, text: str) -> None:
        """Record an agent output and raise if a loop is detected.

        Parameters
        ----------
        task_id:
            The task being executed.
        role:
            The agent role that produced the output. History is tracked
            per ``(task_id, role)`` pair.
        text:
            The raw output text to hash and track.

        Raises
        ------
        GuardrailExceededError
            When ``threshold`` or more of the last ``window`` outputs from
            this role on this task share the same hash.
        """
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        key = (task_id, role)

        if key not in self._history:
            self._history[key] = deque(maxlen=self._window)

        history = self._history[key]
        history.append(digest)

        if len(history) >= self._window:
            # Count occurrences of the most recent hash in the window.
            recent = digest
            count = sum(1 for h in history if h == recent)
            if count >= self._threshold:
                log.warning(
                    "loop_detector.loop_detected",
                    task_id=task_id,
                    role=role,
                    hash=digest,
                    count=count,
                    window=self._window,
                )
                raise GuardrailExceededError(
                    f"loop detected for task {task_id}: same output hash seen "
                    f"{count}/{self._window} times"
                )

    def reset(self, task_id: str) -> None:
        """Clear history for all roles on a task. Safe to call even if never observed."""
        keys_to_remove = [k for k in self._history if k[0] == task_id]
        for k in keys_to_remove:
            del self._history[k]

    def is_tracking(self, task_id: str) -> bool:
        """Return True if there is any history for this task_id (any role)."""
        return any(k[0] == task_id for k in self._history)


__all__ = ["LoopDetector"]
