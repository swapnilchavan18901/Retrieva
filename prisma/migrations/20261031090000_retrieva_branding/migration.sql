-- Preserve the existing admission function and its privileges under the new name.
ALTER FUNCTION filemind_admit_chat(TEXT,TEXT,TEXT,TEXT,TEXT,INTEGER,INTEGER)
    RENAME TO retrieva_admit_chat;
