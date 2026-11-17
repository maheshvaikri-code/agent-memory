"""
Decision Store - Architectural Decision Records (ADRs)
"""

from typing import Any, Dict, List, Optional
from .base import BaseStore
from ..schema import Decision, Alternative, DecisionStatus


class DecisionStore(BaseStore):
    """
    Manages architectural decision memory.

    Tracks:
    - Decisions made and their rationale
    - Alternatives considered and why rejected
    - Constraints decisions impose
    - Agent violations of decisions
    """

    def add_decision(
        self,
        title: str,
        context: str,
        status: str = "accepted",
        deciders: Optional[List[str]] = None,
        consequences_positive: Optional[List[str]] = None,
        consequences_negative: Optional[List[str]] = None,
    ) -> str:
        """Record an architectural decision"""
        dec_id = self._make_id("dec", title)

        dec_id = self._add_memory(
            "decision",
            dec_id,
            title=title,
            context=self._sanitize(context),
            status=status,
            date=self._now(),
            deciders=",".join(deciders or []),
            positives=",".join(consequences_positive or []),
            negatives=",".join(consequences_negative or []),
        )

        return dec_id

    def _decision_id(self, title: str) -> str:
        """Id of the decision titled `title` (see BaseStore._resolve)."""
        return self._resolve("decision", self._make_id("dec", title), title=title)

    def _module_id(self, path: str) -> str:
        """Id of the module at `path` (see BaseStore._resolve)."""
        return self._resolve("module", self._make_id("mod", path), path=path)

    def add_alternative(
        self,
        decision_title: str,
        option: str,
        rejected_because: str
    ) -> str:
        """Record an alternative that was considered"""
        dec_id = self._decision_id(decision_title)
        alt_id = self._make_id("alt", f"{decision_title}_{option}")

        alt_id = self._add_memory(
            "alternative",
            alt_id,
            decision_id=dec_id,
            option=option,
            rejected_because=self._sanitize(rejected_because),
        )

        if self.graph.has_node("decision", dec_id) and not self.graph.has_edge(
            "CONSIDERED_FOR", ("alternative", alt_id), ("decision", dec_id)
        ):
            self.graph.add_edge(
                "CONSIDERED_FOR",
                ("alternative", alt_id),
                ("decision", dec_id)
            )

        return alt_id

    def add_constraint(
        self,
        decision_title: str,
        module_path: str,
        how: str,
        flexibility: str = "medium"
    ) -> None:
        """Record that a decision constrains a module"""
        dec_id = self._decision_id(decision_title)
        mod_id = self._module_id(module_path)

        if self.graph.has_node("decision", dec_id) and self.graph.has_node("module", mod_id):
            self.graph.add_edge(
                "CONSTRAINS",
                ("decision", dec_id),
                ("module", mod_id),
                how=self._sanitize(how),
                flexibility=flexibility
            )

    def record_violation(
        self,
        decision_title: str,
        suggestion: str,
        correction: str
    ) -> None:
        """Record when agent violated a decision"""
        dec_id = self._decision_id(decision_title)

        if self.graph.has_node("decision", dec_id):
            self.graph.add_edge(
                "AGENT_VIOLATED",
                ("agent", "self"),
                ("decision", dec_id),
                when=self._now(),
                suggestion=self._sanitize(suggestion),
                correction=self._sanitize(correction)
            )

    def supersede(self, old_title: str, new_title: str, reason: str) -> None:
        """Mark a decision as superseded by another"""
        old_id = self._decision_id(old_title)
        new_id = self._decision_id(new_title)

        if self.graph.has_node("decision", old_id):
            self.graph.update_node("decision", old_id, status="superseded")

        if self.graph.has_node("decision", old_id) and self.graph.has_node("decision", new_id):
            self.graph.add_edge(
                "SUPERSEDES",
                ("decision", new_id),
                ("decision", old_id),
                reason=self._sanitize(reason),
                date=self._now()
            )

    def check_decision(self, suggestion: str) -> Optional[Dict[str, Any]]:
        """
        Check if a suggestion violates any decisions.

        Returns the conflicting decision if found.
        """
        suggestion_lower = suggestion.lower()

        for node in self.graph.nodes():
            if node.type == "alternative":
                option = node.properties.get("option", "").lower()
                if option and option in suggestion_lower:
                    # Found potential conflict
                    dec_id = node.properties.get("decision_id", "")
                    decision = self._get_node_or_none("decision", dec_id)
                    if decision:
                        return {
                            "decision": decision.properties,
                            "alternative": node.properties,
                            "conflict": f"Suggestion matches rejected alternative: {option}"
                        }

        return None

    def get_constraints_for_module(self, module_path: str) -> List[Dict[str, Any]]:
        """Get all decisions that constrain a module"""
        mod_id = self._module_id(module_path)
        constraints = []

        for edge in self.graph.edges():
            if edge.rel_type == "CONSTRAINS" and edge.target == ("module", mod_id):
                decision = self._get_node_or_none(*edge.source)
                if decision:
                    constraints.append({
                        "decision": decision.properties,
                        "how": edge.properties.get("how", ""),
                        "flexibility": edge.properties.get("flexibility", "medium")
                    })

        return constraints

    def get_relevant(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get relevant decisions for context"""
        results = []

        # Check for modules being modified
        files = context.get("files", [])
        for file_path in files:
            # Find constraints for modules containing this file
            for node in self.graph.nodes():
                if node.type == "module":
                    if file_path.startswith(node.properties.get("path", "")):
                        constraints = self.get_constraints_for_module(node.properties["path"])
                        for c in constraints:
                            results.append({
                                "type": "decision_constraint",
                                "file": file_path,
                                **c
                            })

        # Check task for potential conflicts
        task = context.get("task", "")
        if task:
            conflict = self.check_decision(task)
            if conflict:
                results.append({
                    "type": "potential_conflict",
                    **conflict
                })

        return results

    def get_stats(self) -> Dict[str, int]:
        """Get decision store statistics"""
        decisions = 0
        alternatives = 0
        constraints = 0
        violations = 0

        for node in self.graph.nodes():
            if node.type == "decision":
                decisions += 1
            elif node.type == "alternative":
                alternatives += 1

        for edge in self.graph.edges():
            if edge.rel_type == "CONSTRAINS":
                constraints += 1
            elif edge.rel_type == "AGENT_VIOLATED":
                violations += 1

        return {
            "decisions": decisions,
            "alternatives": alternatives,
            "constraints": constraints,
            "violations": violations,
        }
