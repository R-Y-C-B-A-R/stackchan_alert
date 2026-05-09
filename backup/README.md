# Firmware Backup

The `SHA256SUMS` file in this directory records checksums of full-flash backups.
The binary `.bin` files are excluded from git (16 MB each).

## Creating a backup

Before flashing any custom firmware, read the full 16 MB flash:

```bash
esptool --chip esp32s3 --port /dev/ttyACM0 --baud 460800 \
  read-flash 0x0 0x1000000 flash_$(date +%F).bin
sha256sum flash_$(date +%F).bin >> SHA256SUMS
```

## Restoring the original firmware

```bash
esptool --chip esp32s3 --port /dev/ttyACM0 --baud 460800 \
  write-flash 0x0 flash_<DATE>.bin
```

## Checksums on record

| File                    | Date       | Notes                        |
|-------------------------|------------|------------------------------|
| factory_2026-05-08.bin  | 2026-05-08 | Stock firmware from factory  |
| flash_2026-05-09.bin    | 2026-05-09 | Before alert firmware flash  |
