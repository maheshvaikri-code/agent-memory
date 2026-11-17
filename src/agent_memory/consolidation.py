"""
Memory Consolidation - Strategies for managing memory over time

Handles:
- What to keep vs. forget
- Memory aging and decay
- Consolidation of similar memories
- Importance scoring
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .memory import AgentMemory

from .record import KINDS, MemoryRecord, normalize_text, utc_now


def _parse_utc(timestamp) -> Optional[datetime]:
    """Parse an ISO timestamp to an aware UTC datetime; naive strings are taken as UTC."""
    if not timestamp or not isinstance(timestamp, str):
        return None
    try:
        when = datetime.fromisoformat(timestamp)
    except (ValueError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


class MemoryConsolidator:
    """
    Manages memory consolidation and cleanup.

    Strategies:
    1. Frequency-based: Keep frequently accessed memories
    2. Recency-based: Decay old unused memories
    3. Importance-based: Keep high-impact memories
    4. Reinforcement-based: Strengthen memories that prove useful
    """

    # Memory retention thresholds
    THRESHOLDS = {
        # Type: (min_age_days_to_consider, max_age_days_before_decay)
        "session": (7, 90),           # Sessions decay after 90 days
        "learning_event": (1, 30),    # Learning events decay after 30 days
        "partial_work": (0, 7),       # Partial work urgent, decay after 7 days
        "open_question": (0, 14),     # Open questions decay after 14 days
        "blocker": (0, 7),            # Blockers urgent, decay after 7 days

        # These are kept longer
        "decision": (365, 730),       # Decisions kept 2+ years
        "bug": (180, 365),            # Bugs kept 1+ year
        "bug_pattern": (365, 730),    # Patterns kept 2+ years
        "convention": (365, None),    # Conventions kept indefinitely
        "hot_path": (365, None),      # Hot paths kept indefinitely
        "skill": (90, None),          # Skills kept indefinitely
    }

    # Importance multipliers
    IMPORTANCE = {
        "decision": 1.5,
        "bug_pattern": 1.4,
        "hot_path": 1.3,
        "convention": 1.2,
        "skill": 1.1,
        "session": 0.8,
        "learning_event": 0.7,
        "partial_work": 0.6,
    }

    def __init__(self, memory: "AgentMemory"):
        self.memory = memory

    def consolidate(self) -> Dict[str, int]:
        """
        Run memory consolidation.

        Returns summary of actions taken.
        """
        summary = {
            "decayed": 0,
            "strengthened": 0,
            "merged": 0,
            "archived": 0,
        }

        # 1. Decay old, unused memories
        summary["decayed"] = self._decay_old_memories()

        # 2. Strengthen frequently used memories
        summary["strengthened"] = self._strengthen_used_memories()

        # 3. Merge similar memories
        summary["merged"] = self._merge_similar()

        # 4. Archive completed sessions
        summary["archived"] = self._archive_sessions()

        return summary

    def _decay_old_memories(self) -> int:
        """Remove or mark memories that are too old"""
        decayed = 0
        now = datetime.now(timezone.utc)

        nodes_to_remove = []

        for node in self.memory.graph.nodes():
            node_type = node.type
            thresholds = self.THRESHOLDS.get(node_type)

            if not thresholds:
                continue

            min_age, max_age = thresholds
            if max_age is None:
                continue  # Keep indefinitely

            # Only active records decay; archived/superseded/retracted keep their state.
            # (types outside KINDS, e.g. session, have no record model: read the raw property)
            if node_type in KINDS:
                status = MemoryRecord.from_node(node).status
            else:
                status = node.properties.get("status") or "active"
            if status != "active":
                continue

            # Get node age: envelope created_at first, then legacy properties
            created = (node.properties.get("created_at") or node.properties.get("added")
                       or node.properties.get("started") or node.properties.get("when"))
            created_dt = _parse_utc(created)
            if created_dt is None:
                continue
            try:
                age_days = (now - created_dt).days
            except (ValueError, TypeError):
                continue

            # Check if should decay
            if age_days > max_age:
                # Check importance
                importance = self.IMPORTANCE.get(node_type, 1.0)

                # High importance items get extended retention
                if importance >= 1.2 and age_days < max_age * 1.5:
                    continue

                nodes_to_remove.append((node.type, node.id))
                decayed += 1

        # Archive, never delete: the record stays for history and is not recalled.
        now_iso = utc_now()
        for node_type, node_id in nodes_to_remove:
            self.memory.graph.update_node(node_type, node_id, status="archived",
                                          invalid_at=now_iso, updated_at=now_iso)

        return decayed

    def _strengthen_used_memories(self) -> int:
        """Increase confidence/proficiency of frequently used memories"""
        strengthened = 0

        # Strengthen skills based on application
        for edge in self.memory.graph.edges():
            if edge.rel_type == "SKILL_APPLIED":
                success = edge.properties.get("success") == "True"
                if success:
                    skill_ref = edge.source
                    skill = self.memory.graph.get_node(*skill_ref)
                    if skill:
                        current_prof = float(skill.properties.get("proficiency", 0))
                        # Small increment for each success
                        new_prof = min(1.0, current_prof + 0.02)
                        self.memory.graph.update_node(
                            skill_ref[0],
                            skill_ref[1],
                            proficiency=str(new_prof)
                        )
                        strengthened += 1

        # Strengthen conventions based on learning
        for edge in self.memory.graph.edges():
            if edge.rel_type == "LEARNED_FROM":
                examples = int(edge.properties.get("examples_seen", 0))
                if examples > 10:
                    # Well-learned, could increase confidence
                    strengthened += 1

        return strengthened

    def _merge_similar(self) -> int:
        """Merge active records of the same kind with the same normalised text."""
        merged = 0
        graph = self.memory.graph
        for kind in KINDS:
            groups: Dict[str, List[MemoryRecord]] = {}
            for node in graph.nodes(kind):
                rec = MemoryRecord.from_node(node)
                if rec.status == "active" and rec.text:
                    groups.setdefault(normalize_text(rec.text), []).append(rec)
            for records in groups.values():
                if len(records) < 2:
                    continue
                records.sort(key=lambda r: (-r.confidence, r.created_at or "", str(r.id)))
                keep, rest = records[0], records[1:]
                evidence = [keep.evidence] + [r.evidence for r in rest]
                evidence = "; ".join(dict.fromkeys(e for e in evidence if e))
                now = utc_now()
                graph.update_node(kind, keep.id, confidence=min(0.95, keep.confidence + 0.1),
                                  evidence=evidence, updated_at=now)
                for rec in rest:
                    graph.update_node(kind, rec.id, status="superseded", invalid_at=now, updated_at=now)
                    if not graph.has_edge("SUPERSEDES", keep.ref, rec.ref):
                        graph.add_edge("SUPERSEDES", keep.ref, rec.ref, reason="duplicate", date=now)
                    merged += 1
        return merged

    def _archive_sessions(self) -> int:
        """Archive old completed sessions"""
        archived = 0
        now = datetime.now()
        cutoff = now - timedelta(days=30)

        for node in self.memory.graph.nodes():
            if node.type == "session":
                ended = node.properties.get("ended")
                if ended:
                    try:
                        ended_dt = datetime.fromisoformat(ended)
                        if ended_dt < cutoff:
                            # Mark as archived (don't delete, just flag)
                            self.memory.graph.update_node(
                                "session",
                                node.id,
                                archived="True"
                            )
                            archived += 1
                    except:
                        pass

        return archived

    def get_memory_health(self) -> Dict[str, Any]:
        """Assess overall memory health"""
        stats = self.memory.get_stats()

        # Calculate metrics
        total_nodes = stats["total_nodes"]
        total_edges = stats["total_edges"]

        # Count by age
        now = datetime.now()
        recent = 0  # < 7 days
        medium = 0  # 7-30 days
        old = 0     # > 30 days

        for node in self.memory.graph.nodes():
            created = (
                node.properties.get("added") or
                node.properties.get("started") or
                node.properties.get("when")
            )
            if created:
                try:
                    created_dt = datetime.fromisoformat(created)
                    age = (now - created_dt).days
                    if age < 7:
                        recent += 1
                    elif age < 30:
                        medium += 1
                    else:
                        old += 1
                except:
                    pass

        return {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "age_distribution": {
                "recent_7d": recent,
                "medium_30d": medium,
                "old_30d_plus": old,
            },
            "recommendations": self._get_recommendations(stats),
        }

    def _get_recommendations(self, stats: Dict[str, Any]) -> List[str]:
        """Get recommendations for memory management"""
        recommendations = []

        stores = stats.get("stores", {})

        # Check sessions
        sessions = stores.get("sessions", {})
        if sessions.get("open_questions", 0) > 5:
            recommendations.append("Many open questions - consider resolving or archiving")
        if sessions.get("unresolved_blockers", 0) > 3:
            recommendations.append("Multiple unresolved blockers - needs attention")

        # Check skills
        skills = stores.get("skills", {})
        if skills.get("avg_proficiency", 0) < 0.5:
            recommendations.append("Low average skill proficiency - more practice needed")

        # Check conventions
        conventions = stores.get("conventions", {})
        if conventions.get("total_violations", 0) > 20:
            recommendations.append("High convention violations - review and reinforce")

        # Check bugs
        bugs = stores.get("bugs", {})
        if bugs.get("missed", 0) > bugs.get("prevented", 0):
            recommendations.append("More bugs missed than prevented - review bug patterns")

        return recommendations


class ImportanceScorer:
    """Scores memory importance for retention decisions"""

    @staticmethod
    def score(node, edges: List) -> float:
        """
        Calculate importance score for a node.

        Factors:
        - Type importance
        - Connection count
        - Recency of use
        - Success/failure impact
        """
        base_score = MemoryConsolidator.IMPORTANCE.get(node.type, 1.0)

        # Connection bonus
        connection_count = len([e for e in edges if e.source == (node.type, node.id) or e.target == (node.type, node.id)])
        connection_bonus = min(0.5, connection_count * 0.1)

        # Success bonus for skills
        if node.type == "skill":
            proficiency = float(node.properties.get("proficiency", 0))
            base_score += proficiency * 0.3

        # Severity bonus for bugs
        if node.type == "bug" or node.type == "bug_pattern":
            severity = node.properties.get("severity", "medium")
            if severity == "critical":
                base_score += 0.5
            elif severity == "high":
                base_score += 0.3

        return base_score + connection_bonus
