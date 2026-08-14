// Studio-only presentation and composition owners form an independent lazy
// boundary. They must register before the classic compatibility domain runs,
// but Task Mode should not pay their parse/evaluation cost.
import './orchestration/rich-copy';
import './orchestration/pointer-session';
import './orchestration/graph';
import './orchestration/graph-selection-actions';
import './orchestration/graph-actions';
import './orchestration/selection-focus';
import './orchestration/surface-handoff';
import './orchestration/definition-snapshot';
import './orchestration/studio-api';
import './orchestration/editor-state';
import './orchestration/editor-controller-hub';
