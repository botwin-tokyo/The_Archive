'use strict';
const {z}=require('zod');
module.exports={
vaultStatus:["vault.status",z.object({}).strict()],
vaultHelp:["vault.help",z.object({}).strict()],
vaultEntriesList:["vault.entries.list",z.object({"query":z.string().max(256).optional(),"cursor":z.string().max(256).optional(),"limit":z.number().int().min(1).max(100).optional(),"trashed":z.boolean().optional()}).strict()],
vaultEntriesGet:["vault.entries.get",z.object({"id":z.string().min(1).max(256)}).strict()],
vaultEntriesReveal:["vault.entries.reveal",z.object({"id":z.string().min(1).max(256),"version":z.number().int().min(1).max(9007199254740991).optional()}).strict()],
vaultEntriesCreate:["vault.entries.create",z.object({"requestId":z.string().min(1).max(256),"displayName":z.string().min(1).max(512),"login":z.string().max(16384),"password":z.string().max(16384),"url":z.string().max(4096).optional(),"notes":z.string().max(65536).optional()}).strict()],
vaultEntriesUpdate:["vault.entries.update",z.object({"requestId":z.string().min(1).max(256),"id":z.string().min(1).max(256),"revision":z.number().int().min(1).max(9007199254740991),"displayName":z.string().min(1).max(512).optional(),"url":z.string().max(4096).optional(),"login":z.string().max(16384).optional(),"password":z.string().max(16384).optional(),"notes":z.string().max(65536).optional()}).strict()],
vaultEntriesTrash:["vault.entries.trash",z.object({"requestId":z.string().min(1).max(256),"id":z.string().min(1).max(256),"revision":z.number().int().min(1).max(9007199254740991)}).strict()],
vaultEntriesRestore:["vault.entries.restore",z.object({"requestId":z.string().min(1).max(256),"id":z.string().min(1).max(256),"revision":z.number().int().min(1).max(9007199254740991)}).strict()],
vaultEntriesHistory:["vault.entries.history",z.object({"id":z.string().min(1).max(256),"cursor":z.string().max(256).optional(),"limit":z.number().int().min(1).max(100).optional()}).strict()],
vaultEntriesRestoreVersion:["vault.entries.restoreVersion",z.object({"requestId":z.string().min(1).max(256),"id":z.string().min(1).max(256),"revision":z.number().int().min(1).max(9007199254740991),"version":z.number().int().min(1).max(9007199254740991)}).strict()],
vaultPasswordsGenerate:["vault.passwords.generate",z.object({"length":z.number().int().min(12).max(256).optional(),"alphabet":z.string().min(2).max(256).optional()}).strict()],
vaultRequestsReconcile:["vault.requests.reconcile",z.object({"requestId":z.string().min(1).max(256),"operation":z.string().min(1).max(256)}).strict()],
vaultBackupExport:["vault.backup.export",z.object({}).strict()],
vaultBackupImport:["vault.backup.import",z.object({"requestId":z.string().min(1).max(256),"archive":z.string().max(1000000)}).strict()],
copyVaultSecret:[null,z.object({id:z.string().min(1).max(256),field:z.enum(['login','password'])}).strict()]
};
