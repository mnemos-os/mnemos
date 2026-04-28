"""
Quality Analyzer for Compression Operations
Generates quality manifests tracking what was preserved/removed
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class QualityManifest:
    """Quality assessment result"""
    compression_id: str
    timestamp: str
    source: str  # 'memory_storage', 'rehydration', 'graeae'
    task_type: str
    method: str  # 'apollo', 'artemis', or operator-registered engine id

    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    quality_rating: int  # 0-100%

    quality_summary: Dict[str, Any]
    compression_manifest: Dict[str, Any]

    def to_json(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict"""
        return {
            'compression_id': self.compression_id,
            'timestamp': self.timestamp,
            'source': self.source,
            'task_type': self.task_type,
            'method': self.method,
            'original_tokens': self.original_tokens,
            'compressed_tokens': self.compressed_tokens,
            'compression_ratio': self.compression_ratio,
            'quality_rating': self.quality_rating,
            'quality_summary': self.quality_summary,
            'compression_manifest': self.compression_manifest,
        }


class QualityAnalyzer:
    """Analyze quality loss from compression"""

    # Task-specific quality requirements
    QUALITY_REQUIREMENTS = {
        'security_review': 95,
        'architecture_design': 90,
        'code_generation': 88,
        'reasoning': 85,
        'general': 80,
    }

    def __init__(self, enable_semantic_analysis: bool = True):
        """
        Initialize quality analyzer

        Args:
            enable_semantic_analysis: If True, use embeddings for semantic comparison
                                     If False, use heuristics only (faster)
        """
        self.enable_semantic_analysis = enable_semantic_analysis

        # Try to load semantic analysis tools
        if enable_semantic_analysis:
            try:
                from sentence_transformers import SentenceTransformer
                self.embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
                self.semantic_available = True
            except ImportError:
                logger.warning(
                    "sentence-transformers not available, "
                    "using heuristic quality analysis"
                )
                self.semantic_available = False
        else:
            self.semantic_available = False

    async def analyze(
        self,
        original: str,
        compressed: str,
        task_type: str,
        method: str,
        source: str = 'memory_storage'
    ) -> QualityManifest:
        """
        Generate quality manifest comparing original vs compressed.

        Returns:
          - quality_rating (0-100%)
          - qualitative_summary
          - what_was_removed
          - what_was_preserved
          - risk_factors
          - safe_for / not_safe_for
        """
        from datetime import datetime
        from uuid import uuid4

        compression_id = str(uuid4())
        timestamp = datetime.now().isoformat()

        # 1. Token-level analysis
        original_tokens = self._tokenize(original)
        compressed_tokens = self._tokenize(compressed)

        original_token_count = len(original_tokens)
        compressed_token_count = len(compressed_tokens)
        compression_ratio = compressed_token_count / original_token_count if original_token_count > 0 else 0

        # 2. Semantic analysis (if available)
        semantic_similarity = 100.0  # Default (no semantic analysis)
        if self.semantic_available:
            try:
                semantic_similarity = self._compute_semantic_similarity(
                    original, compressed
                )
            except Exception as e:
                logger.warning(f"Semantic analysis failed: {e}")

        # 3. Entity tracking
        original_entities = self._extract_entities(original)
        compressed_entities = self._extract_entities(compressed)

        preserved_entities = set(original_entities) & set(compressed_entities)
        removed_entities = set(original_entities) - set(compressed_entities)

        # 4. Structure analysis
        original_structure = self._analyze_structure(original)
        compressed_structure = self._analyze_structure(compressed)
        structure_similarity = self._score_structure(
            original_structure, compressed_structure
        )

        # 5. Calculate quality rating
        quality_components = {
            'semantic_similarity': semantic_similarity,  # 0-100
            'entity_preservation': (len(preserved_entities) / len(original_entities) * 100)
                                   if original_entities else 100,
            'structure_preservation': structure_similarity,
        }

        # Weighted average
        quality_rating = int(
            quality_components['semantic_similarity'] * 0.4 +
            quality_components['entity_preservation'] * 0.3 +
            quality_components['structure_preservation'] * 0.3
        )

        # 6. Generate qualitative summary
        quality_summary = {
            'content_preserved': round(semantic_similarity, 1),
            'structure_preserved': round(structure_similarity, 1),
            'key_entities_preserved': len(preserved_entities),
            'total_entities': len(original_entities),
            'entity_loss': len(removed_entities),

            'what_was_removed': self._describe_removals(
                original, compressed, removed_entities
            ),
            'what_was_preserved': self._describe_preservations(
                original, compressed, preserved_entities
            ),

            'risk_factors': self._assess_risks(quality_rating, task_type),
            'safe_for': self._assess_safe_for(quality_rating, task_type),
            'not_safe_for': self._assess_not_safe_for(quality_rating, task_type),
        }

        # 7. Build compression manifest
        compression_manifest = {
            'compression_id': compression_id,
            'timestamp': timestamp,
            'source': source,
            'task_type': task_type,
            'method': method,
            'original_tokens': original_token_count,
            'compressed_tokens': compressed_token_count,
            'compression_ratio': round(compression_ratio, 3),
            'quality_rating': quality_rating,
            'quality_summary': quality_summary,
            'retrieval_info': {
                'full_version_available': True,
                'note': 'Original always stored alongside compressed version'
            }
        }

        return QualityManifest(
            compression_id=compression_id,
            timestamp=timestamp,
            source=source,
            task_type=task_type,
            method=method,
            original_tokens=original_token_count,
            compressed_tokens=compressed_token_count,
            compression_ratio=compression_ratio,
            quality_rating=quality_rating,
            quality_summary=quality_summary,
            compression_manifest=compression_manifest
        )

    # =====================================================================
    # Private helper methods
    # =====================================================================

    def _tokenize(self, text: str) -> List[str]:
        """Simple tokenization"""
        return text.split()

    def _compute_semantic_similarity(self, text1: str, text2: str) -> float:
        """Compute semantic similarity using embeddings (0-100)"""
        try:
            import numpy as np

            emb1 = np.asarray(self.embedding_model.encode(text1))
            emb2 = np.asarray(self.embedding_model.encode(text2))
            # cosine similarity via numpy (avoids sklearn dependency)
            norm = np.linalg.norm(emb1) * np.linalg.norm(emb2)
            similarity = float(np.dot(emb1, emb2) / norm) if norm > 0 else 0.0

            return float(similarity * 100)
        except Exception as e:
            logger.warning(f"Embedding failed: {e}")
            return 85.0  # Conservative default

    def _extract_entities(self, text: str) -> List[str]:
        """Extract named entities (simple heuristic)"""
        # In production, would use NER library (spaCy, etc)
        entities = []

        # Find capitalized words as simple entity detection
        words = text.split()
        for i, word in enumerate(words):
            if word and word[0].isupper() and len(word) > 3:
                entities.append(word.rstrip('.,!?;:'))

        return entities

    def _analyze_structure(self, text: str) -> Dict[str, Any]:
        """Analyze text structure"""
        sentences = [s.strip() for s in text.split('.') if s.strip()]
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]

        return {
            'sentence_count': len(sentences),
            'paragraph_count': len(paragraphs),
            'avg_sentence_length': sum(len(s.split()) for s in sentences) / len(sentences)
                                   if sentences else 0,
            'has_lists': '•' in text or '-' in text,
            'has_code': '```' in text or '`' in text,
        }

    def _score_structure(self, struct1: Dict, struct2: Dict) -> float:
        """Score structure similarity (0-100)"""
        # Simplified scoring
        score = 100.0

        # Penalize sentence count changes > 30%
        if struct1['sentence_count'] > 0:
            sentence_ratio = struct2['sentence_count'] / struct1['sentence_count']
            if sentence_ratio < 0.7:
                score -= (1 - sentence_ratio) * 20

        # Penalize list removal
        if struct1['has_lists'] and not struct2['has_lists']:
            score -= 15

        # Penalize code removal
        if struct1['has_code'] and not struct2['has_code']:
            score -= 20

        return max(0, min(100, score))

    def _describe_removals(
        self,
        original: str,
        compressed: str,
        removed_entities: set
    ) -> List[str]:
        """Describe what was removed"""
        removals = []

        # Sentence count change
        orig_sentences = len([s for s in original.split('.') if s.strip()])
        comp_sentences = len([s for s in compressed.split('.') if s.strip()])
        if orig_sentences > comp_sentences:
            removals.append(
                f"{orig_sentences - comp_sentences} sentences removed"
            )

        # Token reduction
        orig_tokens = len(original.split())
        comp_tokens = len(compressed.split())
        if orig_tokens > comp_tokens:
            removals.append(
                f"~{orig_tokens - comp_tokens} tokens of detail removed"
            )

        # Entity loss
        if removed_entities:
            removals.append(
                f"{len(removed_entities)} named entities removed: "
                f"{', '.join(list(removed_entities)[:3])}"
            )

        return removals if removals else ["Minimal content loss"]

    def _describe_preservations(
        self,
        original: str,
        compressed: str,
        preserved_entities: set
    ) -> List[str]:
        """Describe what was preserved"""
        preserved = []

        # Basic structure
        if original.count('.') > 0 and compressed.count('.') > 0:
            preserved.append("Sentence structure largely intact")

        # Entity preservation
        if preserved_entities:
            preserved.append(
                f"{len(preserved_entities)} key entities preserved"
            )
        else:
            preserved.append("Core concepts preserved through paraphrasing")

        # Code preservation
        if '```' in original and '```' in compressed:
            preserved.append("Code blocks preserved")

        return preserved

    def _assess_risks(self, quality_rating: int, task_type: str) -> List[str]:
        """What are the risks of using the compressed version?"""
        risks = []

        if quality_rating < 80:
            risks.append("Significant detail loss - use with caution")

        if task_type == 'architecture_design' and quality_rating < 85:
            risks.append("Design complexity may be underrepresented")

        if task_type == 'code_generation' and quality_rating < 90:
            risks.append("Edge cases and error handling may be missing")

        if task_type == 'security_review' and quality_rating < 95:
            risks.append("CRITICAL: Security implications may be missed")

        return risks if risks else ["Low risk - quality is acceptable"]

    def _assess_safe_for(self, quality_rating: int, task_type: str) -> List[str]:
        """What is this safe for?"""
        safe_for = []

        if quality_rating >= 85:
            safe_for.append("Initial consultation")
            safe_for.append("Quick decision making")

        if quality_rating >= 80:
            safe_for.append("Pattern recognition")
            safe_for.append("Brainstorming")

        if quality_rating >= 90:
            safe_for.append("Detailed analysis")
            safe_for.append("Final decision making")

        return safe_for if safe_for else ["General reference only"]

    def _assess_not_safe_for(self, quality_rating: int, task_type: str) -> List[str]:
        """What should NOT use compressed version?"""
        not_safe = []

        if quality_rating < 95:
            not_safe.append("Regulatory documentation")

        if quality_rating < 90:
            not_safe.append("Detailed technical review")
            not_safe.append("Full audit trail requirements")
            not_safe.append("Security-critical decisions")

        if quality_rating < 85:
            not_safe.append("Complex system design")
            not_safe.append("Algorithm selection")

        return not_safe if not_safe else ["Safe for most purposes"]
