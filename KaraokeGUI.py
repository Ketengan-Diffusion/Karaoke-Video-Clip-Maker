import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext
import os
import subprocess
import re
import shlex
import shutil
import threading
from moviepy.editor import VideoFileClip
import whisper
from stable_whisper import modify_model
from demucs.separate import main as demucs_main
import traceback
from pytube import YouTube

def downmix(wav):
    if wav.ndim == 2:
        wav = wav.mean(dim=0)
    return wav

def separate_sources(audio_path, output_path, status_callback):
    print("Initiating separate_sources function...")
    try:
        models = ['htdemucs_ft']
        separated_dir = os.path.join(output_path, 'separated')
        os.makedirs(separated_dir, exist_ok=True)

        for model_name in models:
            model_dir = os.path.join(separated_dir, model_name)
            os.makedirs(model_dir, exist_ok=True)

            # Ensuring Demucs outputs to the correct directory / Memastikan Demucs mengeluarkan file audio terpisah di direktori yang benar
            current_dir = os.getcwd()
            os.chdir(model_dir)
            try:
                args = f'-n {model_name} --two-stems vocals --float32 "{audio_path}"'
                demucs_main(shlex.split(args))
                print(f"Processed with model: {model_name}")

                # Rename and save the tracks / Merubah nama file dan menyimpan file suara
                demucs_output_dir = os.path.join(model_dir, 'separated', model_name, 'extracted_audio')
                os.replace(os.path.join(demucs_output_dir, 'vocals.wav'), 
                           os.path.join(model_dir, 'vocals_final.wav'))
                os.replace(os.path.join(demucs_output_dir, 'no_vocals.wav'),  
                           os.path.join(model_dir, 'instruments_final.wav'))

                print(f"Files from {model_name} saved successfully.")
            finally:
                os.chdir(current_dir)
    except Exception as e:
        print(f"An error occurred in separate_sources: {str(e)}")
        return False
    return True

def adjust_subtitle_timing(ass_content, offset_ms):
    timestamp_pattern = re.compile(r'(\d+):(\d+):(\d+)\.(\d+)')
    
    def adjust_timestamp(match):
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        milliseconds = int(match.group(4)) * 10

        total_ms = ((hours * 3600) + (minutes * 60) + seconds) * 1000 + milliseconds + offset_ms
        if total_ms < 0:
            total_ms = 0

        new_hours = total_ms // 3600000
        total_ms %= 3600000
        new_minutes = total_ms // 60000
        total_ms %= 60000
        new_seconds = total_ms // 1000
        new_centiseconds = (total_ms % 1000) // 10

        return f"{new_hours}:{new_minutes:02}:{new_seconds:02}.{new_centiseconds:02}"

    adjusted_ass_content = timestamp_pattern.sub(adjust_timestamp, ass_content)
    return adjusted_ass_content

def split_long_lines(ass_content):
    def split_line(line):
        parts = line.split(',')
        if len(parts) < 10:
            return line

        text = parts[9]
        max_length = 80  # Maximum number of characters per line / Jumlah maksimal karakter per baris

        words = text.split(' ')
        lines = []
        current_line = ""

        for word in words:
            if len(current_line) + len(word) + 1 <= max_length:
                if current_line:
                    current_line += " " + word
                else:
                    current_line = word
            else:
                lines.append(current_line)
                current_line = word
        if current_line:
            lines.append(current_line)

        # Ensure a maximum of two lines per subtitle / Memastikan terdapat maksimal dua baris teks dalam satu segmen
        if len(lines) > 2:
            lines = lines[:2]

        parts[9] = r'\N'.join(lines)
        return ','.join(parts)

    lines = ass_content.split('\n')
    split_lines = [split_line(line) for line in lines]
    return '\n'.join(split_lines)

def generate_karaoke_subtitles(audio_path, output_path, model_name, status_callback, offset_ms=750):
    # Generate karaoke lyrics and effect using whisper / Melakukan lirik dan efek lirik karaoke menggunakan Whisper
    model = whisper.load_model(model_name)
    modify_model(model)
    result = model.transcribe(audio_path, suppress_silence=True, vad=True, demucs=True, ts_num=16)

     # Fix timestamp format in the temporary .ass file / Penyesuaian format "timestamp" di file .ass sementara
    temp_ass_path = os.path.join(os.path.dirname(output_path), "temp.ass")
    result.to_ass(temp_ass_path)

    with open(temp_ass_path, 'r') as f:
        ass_content = f.read()

    timestamp_pattern = re.compile(r'(?:(\d+):)?(\d+):(\d+)\.(\d+)')

    # Function to convert Whisper's timestamp format to ASS format / Fungsi untu mengubah format waktu dari Whisper's ke format yang didukung file .ass
    def convert_timestamp(match):
        hours = match.group(1) or "0"
        minutes = int(match.group(2))
        seconds = int(match.group(3))
        milliseconds = int(match.group(4))
        centiseconds = milliseconds // 10

        return f"{int(hours)}:{minutes:02}:{seconds:02}.{centiseconds:02}"

    ass_content = timestamp_pattern.sub(convert_timestamp, ass_content)
    ass_content = adjust_subtitle_timing(ass_content, offset_ms)
    ass_content = split_long_lines(ass_content)

    with open(temp_ass_path, 'w') as f:
        f.write(ass_content)

    # Save the modified .ass file / Menyimpan file .ass yang sudah dimodifikasi
    os.rename(temp_ass_path, output_path)
    print(f"Subtitle file saved to: {output_path}")

def merge_audio_video(video_path, audio_path, temp_ass_path, output_path, overwrite, status_callback):
    # Convert command formating into ffmpeg acceptable format for filtered subtitle / Merubah format perintah ke format yang dapat diterima oleh ffmpeg untuk subtitle yang memiliki efek
    escaped_temp_ass_path = temp_ass_path.replace("\\", "/").replace(":", "\\:")
    # Command construction for ffmpeg / Format perintah untuk ffmpeg
    command = [
        'ffmpeg',
        '-i', video_path,
        '-i', audio_path, 
        '-i', video_path,
        '-vf', f"ass='{escaped_temp_ass_path}'",
        '-map', '0:v:0',
        '-map', '1:a:0',
        '-map', '2:a:0',
        '-c:v', 'libx264',
        '-c:a', 'aac',
        '-metadata:s:a:0', 'title=Instrumental',
        '-metadata:s:a:1', 'title=Original',
    ]

    if overwrite:
        command.append('-y')
    else:
        command.append('-n')

    command.append(output_path)

    print("Running command: " + ' '.join(command))
    try:
        subprocess.run(command, check=True)
        print("Command executed successfully.")
    except subprocess.CalledProcessError as e:
        print(f"Command failed with error {e.returncode}.")

class KaraokeApp:
    def __init__(self, root):
        self.root = root
        root.title("Karaoke Video Maker")

        self.frame = tk.Frame(root)
        self.frame.pack(padx=10, pady=10)

        tk.Label(self.frame, text="Video File:").pack()
        self.video_path_entry = tk.Entry(self.frame, width=50)
        self.video_path_entry.pack()
        tk.Button(self.frame, text="Browse", command=self.load_video).pack()

        self.use_youtube_var = tk.BooleanVar()
        self.use_youtube_checkbox = tk.Checkbutton(self.frame, text="Use YouTube source instead", variable=self.use_youtube_var, command=self.toggle_youtube_source)
        self.use_youtube_checkbox.pack()

        tk.Label(self.frame, text="Output Directory:").pack()
        self.output_dir_entry = tk.Entry(self.frame, width=50)
        self.output_dir_entry.pack()
        tk.Button(self.frame, text="Browse", command=self.select_output_directory).pack()

        tk.Label(self.frame, text="Whisper Model:").pack()
        self.model_var = tk.StringVar(self.frame)
        self.model_var.set("large-v3")  # default value / nilai bawaan
        self.model_dropdown = tk.OptionMenu(self.frame, self.model_var, "tiny", "base", "small", "medium", "large-v3")
        self.model_dropdown.pack()

        self.correct_lyrics_var = tk.BooleanVar()
        self.correct_lyrics_checkbox = tk.Checkbutton(self.frame, text="Correct lyrics before merging?", variable=self.correct_lyrics_var, command=self.toggle_lyrics_editing)
        self.correct_lyrics_checkbox.pack()

        self.lyrics_editor = scrolledtext.ScrolledText(self.frame, width=80, height=20)
        self.lyrics_editor.pack(pady=10)
        self.lyrics_editor.pack_forget()

        self.save_lyrics_btn = tk.Button(self.frame, text="Save and Continue to Merging", command=self.save_lyrics)
        self.save_lyrics_btn.pack(pady=10)
        self.save_lyrics_btn.pack_forget()

        self.start_btn = tk.Button(self.frame, text="Start Processing", command=self.start_processing)
        self.start_btn.pack(pady=20)

        self.stop_btn = tk.Button(self.frame, text="Stop Processing", command=self.stop_processing, state=tk.DISABLED)
        self.stop_btn.pack()

        self.status_label = tk.Label(self.frame, text="", fg='blue')
        self.status_label.pack(pady=10)

        self.video_path = None
        self.audio_output_path = None
        self.subtitles_path = None
        self.vocals_path = None
        self.instruments_path = None

    def load_video(self):
        self.video_path = filedialog.askopenfilename(filetypes=[("Video files", "*.mp4 *.avi *.mov")])
        self.video_path_entry.delete(0, tk.END)
        self.video_path_entry.insert(0, self.video_path)

    def select_output_directory(self):
        directory = filedialog.askdirectory()
        self.output_dir_entry.delete(0, tk.END)
        self.output_dir_entry.insert(0, directory)

    def toggle_youtube_source(self):
        if self.use_youtube_var.get():
            self.video_path_entry.config(state=tk.NORMAL)
        else:
            self.video_path_entry.config(state=tk.NORMAL)

    def toggle_lyrics_editing(self):
        if self.correct_lyrics_var.get():
            self.lyrics_editor.pack()
            self.save_lyrics_btn.pack()
        else:
            self.lyrics_editor.pack_forget()
            self.save_lyrics_btn.pack_forget()

    def update_status(self, message):
        self.status_label.config(text=message)
        self.root.update_idletasks()

    def start_processing(self):
        self.start_btn['state'] = tk.DISABLED
        self.stop_btn['state'] = tk.NORMAL
        self.update_status("Extracting the audio...")

        threading.Thread(target=self.process_video).start()

    def process_video(self):
        try:
            video_path = self.video_path_entry.get()
            output_dir = self.output_dir_entry.get()
            model_name = self.model_var.get()

            if self.use_youtube_var.get():
                video_path = self.download_youtube_video(video_path, output_dir)
                self.video_path_entry.delete(0, tk.END)
                self.video_path_entry.insert(0, video_path)

            self.audio_output_path = os.path.join(output_dir, 'extracted_audio.wav')
            self.extract_audio(video_path, self.audio_output_path)

            self.update_status("Removing the vocal...")
            demucs_success = separate_sources(self.audio_output_path, output_dir, self.update_status)

            if demucs_success:
                self.vocals_path = os.path.join(output_dir, 'separated', 'htdemucs_ft', 'vocals_final.wav')
                self.instruments_path = os.path.join(output_dir, 'separated', 'htdemucs_ft', 'instruments_final.wav')
                self.subtitles_path = os.path.join(output_dir, 'output.ass')

                self.update_status("Transcribing the lyrics...")
                generate_karaoke_subtitles(self.vocals_path, self.subtitles_path, model_name, self.update_status)
                print("Lyrics transcription completed.")

                if self.correct_lyrics_var.get():
                    self.lyrics_editor.delete(1.0, tk.END)
                    with open(self.subtitles_path, 'r') as f:
                        self.lyrics_editor.insert(tk.END, f.read())
                    self.toggle_lyrics_editing()
                else:
                    self.continue_merging()
            else:
                raise Exception("Demucs processing failed.")
        except Exception as e:
            self.update_status("Processing failed.")
            messagebox.showerror("Error", f"An error occurred: {e}\n{traceback.format_exc()}")
            self.reset_buttons()
        finally:
            self.update_status("Processing Completed.")

    def download_youtube_video(self, url, output_dir):
        self.update_status("Downloading YouTube video...")
        try:
            yt = YouTube(url)
            stream = yt.streams.filter(file_extension='mp4').first()
            output_path = stream.download(output_path=output_dir, filename='input_video.mp4')
            print(f"Downloaded video to: {output_path}")
            return output_path
        except Exception as e:
            print("Failed to download YouTube video:", e)
            raise

    def extract_audio(self, video_path, audio_output_path):
        try:
            video = VideoFileClip(video_path)
            audio = video.audio
            audio.write_audiofile(audio_output_path)
            print("Audio extracted to:", audio_output_path)
        except Exception as e:
            print("Failed to extract audio:", e)
            raise

    def save_lyrics(self):
        with open(self.subtitles_path, 'w') as f:
            f.write(self.lyrics_editor.get(1.0, tk.END))
        self.toggle_lyrics_editing()
        self.continue_merging()

    def continue_merging(self):
        output_video_path = os.path.join(self.output_dir_entry.get(), 'output.mp4')
        overwrite = False
        if os.path.exists(output_video_path):
            if not messagebox.askyesno("File Exists", f"File '{output_video_path}' already exists. Overwrite?"):
                self.update_status("Processing aborted by user.")
                self.reset_buttons()
                return
            else:
                overwrite = True

        self.update_status("Finalizing...")
        instruments_path = os.path.join(self.output_dir_entry.get(), 'separated', 'htdemucs_ft', 'instruments_final.wav')
        merge_audio_video(self.video_path_entry.get(), instruments_path, self.subtitles_path, output_video_path, overwrite, self.update_status)
        print("Merging completed.")
        self.cleanup_temp_files()
        messagebox.showinfo("Success", "Processing completed successfully!")
        self.reset_buttons()

    def cleanup_temp_files(self):
        try:
            os.remove(self.audio_output_path)
            os.remove(self.subtitles_path)
            shutil.rmtree(os.path.join(self.output_dir_entry.get(), 'separated'))
            print("Temporary files cleaned up.")
        except Exception as e:
            print("Failed to clean up temporary files:", e)

    def reset_buttons(self):
        self.start_btn['state'] = tk.NORMAL
        self.stop_btn['state'] = tk.DISABLED

    def stop_processing(self):
        self.update_status("Processing was stopped.")
        self.stop_btn['state'] = tk.DISABLED
        self.start_btn['state'] = tk.NORMAL

if __name__ == "__main__":
    root = tk.Tk()
    app = KaraokeApp(root)
    root.mainloop()