"""
Codebase Store - Mental model of the codebase architecture
"""

from typing import Any, Dict, List, Optional
from .base import BaseStore
from ..schema import Module, Abstraction, DesignPattern


class CodebaseStore(BaseStore):
    """
    Manages codebase mental model memory.

    Tracks:
    - Module purposes and relationships
    - Abstractions spanning modules
    - Design patterns used
    - Agent's confidence in understanding
    """

    def add_module(
        self,
        path: str,
        purpose: str,
        complexity: str = "medium",
        stability: str = "stable",
        **properties
    ) -> str:
        """Add or update a module in the mental model"""
        module_id = self._make_id("mod", path)

        module_id = self._add_memory(
            "module",
            module_id,
            path=path,
            purpose=self._sanitize(purpose),
            complexity=complexity,
            stability=stability,
            last_visited=self._now(),
            **{k: str(v) for k, v in properties.items()}
        )

        return module_id

    def _module_id(self, path: str) -> str:
        """Id of the module at `path` (see BaseStore._resolve)."""
        return self._resolve("module", self._make_id("mod", path), path=path)

    def add_dependency(
        self,
        from_module: str,
        to_module: str,
        coupling: str = "loose",
        critical_path: bool = False
    ) -> None:
        """Record dependency between modules"""
        from_id = self._module_id(from_module)
        to_id = self._module_id(to_module)

        if self.graph.has_node("module", from_id) and self.graph.has_node("module", to_id):
            self.graph.add_edge(
                "DEPENDS_ON",
                ("module", from_id),
                ("module", to_id),
                coupling=coupling,
                critical_path=str(critical_path)
            )

    def add_abstraction(
        self,
        name: str,
        level: str,
        modules: List[str],
        documentation: Optional[str] = None
    ) -> str:
        """Add a high-level abstraction"""
        abs_id = self._make_id("abs", name)

        props = {
            "name": name,
            "level": level,
            "modules": ",".join(modules),
        }
        if documentation:
            props["documentation"] = documentation

        self.graph.add_node("abstraction", abs_id, **props)

        # Link to modules
        for mod in modules:
            mod_id = self._module_id(mod)
            if self.graph.has_node("module", mod_id):
                self.graph.add_edge(
                    "ABSTRACTS",
                    ("abstraction", abs_id),
                    ("module", mod_id)
                )

        return abs_id

    def add_pattern(
        self,
        name: str,
        modules: List[str],
        why_chosen: Optional[str] = None
    ) -> str:
        """Record a design pattern usage"""
        pattern_id = self._make_id("dp", name)

        pattern_id = self._add_memory(
            "design_pattern",
            pattern_id,
            name=name,
            where_used=",".join(modules),
            why_chosen=why_chosen or ""
        )

        # Link to modules
        for mod in modules:
            mod_id = self._module_id(mod)
            if self.graph.has_node("module", mod_id) and not self.graph.has_edge(
                "IMPLEMENTS", ("module", mod_id), ("design_pattern", pattern_id)
            ):
                self.graph.add_edge(
                    "IMPLEMENTS",
                    ("module", mod_id),
                    ("design_pattern", pattern_id)
                )

        return pattern_id

    def update_confidence(
        self,
        module_path: str,
        confidence: float,
        sessions_built: int = 1
    ) -> None:
        """Update agent's confidence in understanding a module"""
        mod_id = self._module_id(module_path)

        if not self.graph.has_node("module", mod_id):
            return

        # Update or create agent understanding edge
        self.graph.add_edge(
            "MENTAL_MODEL_CONFIDENCE",
            ("agent", "self"),
            ("module", mod_id),
            confidence=str(confidence),
            sessions_built=str(sessions_built),
            last_validated=self._now()
        )

    def get_module_context(self, module_path: str) -> Dict[str, Any]:
        """
        Get full context for a module before modifying it.

        Returns dependencies, patterns, abstractions, and confidence.
        """
        mod_id = self._module_id(module_path)
        context = {
            "module": None,
            "dependencies": [],
            "dependents": [],
            "patterns": [],
            "abstractions": [],
            "confidence": 0.0,
        }

        if not self.graph.has_node("module", mod_id):
            return context

        # Get module
        node = self._get_node_or_none("module", mod_id)
        if node:
            context["module"] = node.properties

        # Get dependencies (what this module depends on)
        for edge in self.graph.edges():
            if edge.rel_type == "DEPENDS_ON" and edge.source == ("module", mod_id):
                dep = self._get_node_or_none(*edge.target)
                if dep:
                    context["dependencies"].append(dep.properties)

            # Get dependents (what depends on this module)
            if edge.rel_type == "DEPENDS_ON" and edge.target == ("module", mod_id):
                dep = self._get_node_or_none(*edge.source)
                if dep:
                    context["dependents"].append(dep.properties)

            # Get patterns
            if edge.rel_type == "IMPLEMENTS" and edge.source == ("module", mod_id):
                pattern = self._get_node_or_none(*edge.target)
                if pattern:
                    context["patterns"].append(pattern.properties)

            # Get abstractions
            if edge.rel_type == "ABSTRACTS" and edge.target == ("module", mod_id):
                abs_node = self._get_node_or_none(*edge.source)
                if abs_node:
                    context["abstractions"].append(abs_node.properties)

            # Get confidence
            if edge.rel_type == "MENTAL_MODEL_CONFIDENCE" and edge.target == ("module", mod_id):
                context["confidence"] = float(edge.properties.get("confidence", 0))

        return context

    def get_relevant(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Get relevant codebase knowledge for context"""
        results = []

        # If files specified, get their module context
        files = context.get("files", [])
        for file_path in files:
            # Find module containing this file
            for node in self.graph.nodes():
                if node.type == "module":
                    if file_path.startswith(node.properties.get("path", "")):
                        mod_ctx = self.get_module_context(node.properties["path"])
                        results.append({
                            "type": "module_context",
                            "file": file_path,
                            **mod_ctx
                        })
                        break

        return results

    def get_stats(self) -> Dict[str, int]:
        """Get codebase store statistics"""
        modules = 0
        abstractions = 0
        patterns = 0
        dependencies = 0

        for node in self.graph.nodes():
            if node.type == "module":
                modules += 1
            elif node.type == "abstraction":
                abstractions += 1
            elif node.type == "design_pattern":
                patterns += 1

        for edge in self.graph.edges():
            if edge.rel_type == "DEPENDS_ON":
                dependencies += 1

        return {
            "modules": modules,
            "abstractions": abstractions,
            "patterns": patterns,
            "dependencies": dependencies,
        }
