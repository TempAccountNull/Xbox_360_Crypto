#!/usr/bin/env python3

from os import name
from typing import Union
from pathlib import Path
from shutil import copyfile, move
from subprocess import Popen, PIPE
from argparse import ArgumentParser
from tempfile import TemporaryDirectory
from ctypes import LittleEndianStructure, sizeof, c_ubyte, c_uint16, c_uint32, c_uint64

from XeCrypt import *
from StreamIO import *

# constants
BUFF_SIZE = 4096
XENON_DATA_SIZE = 0x1C
WORK_DIR = None
CACHE_DIR = "cache"
TMP_CAB_FILE = "tmp.cab"

# aliases
c_word = c_uint16
c_dword = c_uint32
c_qword = c_uint64

class CAB_HEADER(LittleEndianStructure):
	_fields_ = [
		("magic", c_dword),
		("cksm_hdr", c_dword),
		("cb_cabinet", c_dword),
		("cksm_folders", c_dword),
		("off_files", c_dword),
		("cksm_files", c_dword),
		("version", c_word),
		("cnt_folders", c_word),
		("cnt_files", c_word),
		("cnt_flags", c_word),
		("flags", c_word),
		("set_id", c_word),
		("i_cabinet", c_word),
	]

class CAB_FOLDER(LittleEndianStructure):
	_fields_ = [
		("off_cab_start", c_dword),
		("cf_data", c_word),
		("type_compress", c_word)
		#("xenon_data", c_ubyte * 0x1C)
	]

class CAB_ENTRY(LittleEndianStructure):
	_fields_ = [
		("cb_file", c_dword),
		("off_folder_start", c_dword),
		("i_folder", c_word),
		("date", c_word),
		("time", c_word),
		("attribs", c_word),
	]

class CAB_DATA(LittleEndianStructure):
	_fields_ = [
		("cksm", c_dword),
		("cb_data", c_word),
		("cb_uncomp", c_word)
	]

class RC4_SHA_HEADER(LittleEndianStructure):
	_fields_ = [
		("cksm", c_ubyte * 0x14),
		("confounder", c_ubyte * 8)
	]

def path_type(s: str) -> Path:
	return Path(s)

def extract_cab(filename: str, path: str) -> bool:
	if name == "nt":
		p = Popen(["expand", filename, "-F:*", path], stdout=PIPE, stderr=PIPE)
	elif name == "posix":
		p = Popen(["cabextract", "-d", path, "-F", "*", filename], stdout=PIPE, stderr=PIPE)
	else:
		raise NotImplemented("Only Windows and Linux are supported!")
	(out, err) = p.communicate()
	p.wait()
	return p.returncode == 0

def stream_decrypt_with_struct(xcp: StreamIO, key: Union[bytes, bytearray], struct_offset: int, data_offset: int, size: int) -> bytes:
	xcp.offset = struct_offset
	rc4_sha_struct = xcp.read_struct(RC4_SHA_HEADER)
	cipher = XeCryptRc4.new(XeCryptHmacSha(key, bytes(rc4_sha_struct.cksm)))
	cipher.decrypt(bytes(rc4_sha_struct.confounder))

	# xcp.offset = data_offset
	# dec_data = cipher.decrypt(xcp.read(size))
	# xcp.offset = data_offset
	# xcp.write(dec_data)

	dec_data = xcp.perform_function_at(data_offset, size, cipher.decrypt)

	return dec_data

def main() -> int:
	# setup arguments
	parser = ArgumentParser(description="A script to decrypt, merge, and convert XCP files for the Xbox 360")
	parser.add_argument("input", type=path_type, help="The file to extract")
	parser.add_argument("-k", "--key", type=str, help="The key used to decrypt the XCP package")
	parser.add_argument("--ignore", action="store_true", help="Ignore the file overwrite warning")
	parser.add_argument("--no-backup", action="store_true", help="Disable backups")
	# parse arguments
	args = parser.parse_args()

	# validate arguments
	assert args.input.is_file(), "The specified input file or directory doesn't exist"
	if args.key and len(args.key) % 2 == 0:
		args.key = bytes.fromhex(args.key)

	# create work directory
	with TemporaryDirectory(f"_{args.input.stem.upper()}_XCP") as work_dir:
		work_dir = Path(work_dir)
		out_dir = args.input.parents[0]

		if not args.ignore:
			input("This will OVERWRITE and DELETE the original XCP file, press \"ENTER\" if you want to continue...")

		if not args.no_backup:
			print("Backing up the input file...")
			copyfile(args.input, out_dir / (args.input.stem.upper() + ".bak"))
		else:
			print("Skipping backup...")

		print("Decrypting XCP file...")
		with StreamIO(str(args.input), Endian.LITTLE) as xcp:
			print("Decrypting CAB header...")
			cab_hdr_data = stream_decrypt_with_struct(xcp, args.key, 0x60, 0, 0x60)
			cab_hdr_struct = CAB_HEADER.from_buffer_copy(cab_hdr_data)
			assert cab_hdr_struct.magic in (0x4D534346, 0x4643534D), "Invalid key specified"
			print("Key appears to be OK!")

			print("Decrypting folder data...")
			folder_size = cab_hdr_struct.cnt_folders * (sizeof(CAB_FOLDER) + XENON_DATA_SIZE)
			stream_decrypt_with_struct(xcp, args.key, 0x28, 0x180, folder_size)

			print("Decrypting filenames...")
			xcp.offset = 0x44
			rc4_sha_struct = xcp.read_struct(RC4_SHA_HEADER)
			cipher = XeCryptRc4.new(XeCryptHmacSha(args.key, bytes(rc4_sha_struct.cksm)))
			cipher.decrypt(bytes(rc4_sha_struct.confounder))
			# decrypt filenames
			xcp.offset = cab_hdr_struct.off_files
			for i in range(cab_hdr_struct.cnt_files):
				# decrypt header
				xcp.perform_function_at(xcp.offset, sizeof(CAB_ENTRY), cipher.decrypt)

				xcp.offset += sizeof(CAB_ENTRY)

				# decrypt filename
				idx = 0
				byt = 0
				while byt != 0 or idx == 0:
					byt = xcp.perform_function_at(xcp.offset, 1, cipher.decrypt)[0]
					xcp.offset += 1
					idx += 1
				# rename the files to extract them properly
				xcp.write_bytes_at(xcp.offset - 4, str(i).zfill(3).encode("UTF8"))

			print("Parsing folders...")
			xcp.offset = 0x180
			folders = []
			for i in range(cab_hdr_struct.cnt_folders):
				xcp.set_label(f"folder{i}")
				folders.append(xcp.read_struct(CAB_FOLDER))
				xcp.seek(XENON_DATA_SIZE, SEEK_CUR)

			print("Decrypting folders...")
			for i in range(cab_hdr_struct.cnt_folders):
				curr_folder = folders[i]

				if (i + 1) == cab_hdr_struct.cnt_folders:
					size = xcp.length() - curr_folder.off_cab_start
				else:
					next_folder = folders[i + 1]
					size = next_folder.off_cab_start - curr_folder.off_cab_start

				stream_decrypt_with_struct(xcp, args.key, xcp.get_label(f"folder{i}") + sizeof(CAB_FOLDER), curr_folder.off_cab_start, size)

		if not (work_dir / CACHE_DIR).is_dir():
			print("Creating output directory...")
			(work_dir / CACHE_DIR).mkdir()

		print("Renaming XCP file...")
		if (work_dir / TMP_CAB_FILE).is_file():
			(work_dir / TMP_CAB_FILE).unlink()
		# args.input.rename(work_dir / TMP_CAB_FILE)
		move(args.input, work_dir / TMP_CAB_FILE)

		print("Extracting CAB file...")
		# extract the cabinet file and make sure it exited properly
		assert extract_cab(str(work_dir / TMP_CAB_FILE), str(work_dir / CACHE_DIR)), "CAB extraction failed!"

		print("Merging the extracted files...")
		# merge the files together to make them into one file
		with StreamIO(str(work_dir / (args.input.stem.upper()))) as fw:
			for single in (work_dir / CACHE_DIR).iterdir():
				with (work_dir / CACHE_DIR / single).open("rb") as fr:
					while True:
						buff = fr.read(BUFF_SIZE)
						if not buff:
							break
						fw.write(buff)
			print("Converting to a LIVE package...")
			# really hacky way to make it a LIVE file
			fw.write_bytes_at(0, b"LIVE")

		print("Moving file...")
		# (work_dir / (args.input.stem.upper())).rename(args.input.parents[0] / args.input.stem.upper())
		move(work_dir / (args.input.stem.upper()), out_dir / args.input.stem.upper())

		print("Done!")

	return 0

if __name__ == "__main__":
	exit(main())