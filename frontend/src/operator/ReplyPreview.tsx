import { useState } from 'react';
import { pages } from '../glasses/draft';
import '../glasses/hud.css';

export default function ReplyPreview({ text }: { text: string }) {
  const [page, setPage] = useState(0);
  const content = pages(text);
  const current = Math.min(page, content.length - 1);
  return <>
    <div className="glasses-preview" aria-label="Last operator reply glasses preview">
      <div className="hud-screen">
        <div className="hud-header"><span>EVENCOMMS · REPLY</span><span>{current + 1} / {content.length}</span></div>
        <div className="hud-body">{content[current] || 'Waiting for an operator reply.'}</div>
        <div className="hud-footer"><span>{text ? 'REPLY' : 'READY'}</span><span>Hold to talk · Tap to send</span></div>
      </div>
    </div>
    {content.length > 1 && <div className="op-preview-pages" aria-label="Reply preview pages">
      <button type="button" className="op-text-button" aria-label="Previous reply preview page" disabled={current === 0} onClick={() => setPage(current - 1)}>Previous</button>
      <span className="op-fine-print" role="status">Page {current + 1} of {content.length}</span>
      <button type="button" className="op-text-button" aria-label="Next reply preview page" disabled={current === content.length - 1} onClick={() => setPage(current + 1)}>Next</button>
    </div>}
  </>;
}
