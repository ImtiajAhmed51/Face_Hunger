import { mutate } from './api';

/** Server-side audited actions return an audit_id; undoing replays the stored inverse. */
export const undoAudit = (auditId: number) => () => mutate(`/audit/${auditId}/undo`);

export interface Audited { audit_id: number }
