# Accessibility

The graph console targets WCAG 2.1 AA.

## Keyboard controls

- `Tab`: move through controls, node bodies, node ports, SVG edges, and the text edge list.
- Arrow keys on a node: move it by 5 pixels.
- Shift plus arrow: move it by 20 pixels.
- `Ctrl+Enter` on a node: start a connection.
- Enter on another node port: finish the connection.
- `Escape`: cancel connection or selection.
- `Delete` on a node: delete it.
- Canvas buttons: zoom in, zoom out, and fit.

## Nonvisual graph view

Every edge appears in the Connections list with source, target, condition, and loop limit. Run state also appears in a table and an ARIA live region. The canvas is not the only source of graph information.

Pointer users can drag from a node's output port to another node's input port. The canvas shows a connection preview and valid or invalid target state. The live region also states invalid-drop and cancellation reasons. The existing `C`, `Enter`, and `Escape` path provides the same operation without pointer input.

## Visual rules

- Text uses high-contrast light colors on dark opaque panel layers.
- Focus uses a three-pixel light outline outside the control.
- Status does not rely on color alone.
- Reduced-motion preferences disable interface animation and transitions.
- The layout reflows into one column on narrow screens.

## Error handling

Graph errors appear as text in the inspector and live status. Invalid cycles identify the exact edge and required loop field. Bad conditions show the accepted grammar. The server rejects an invalid graph before simulation.

## Manual release checks

1. Complete node creation, connection, edge editing, validation, simulation, import, and export without a pointer.
2. Read the page with NVDA on Windows and VoiceOver on macOS.
3. Confirm visible focus at 200% zoom.
4. Confirm text and controls remain usable with reduced motion and forced colors.
5. Confirm the connection list matches the canvas.
