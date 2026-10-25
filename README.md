# metawipe

Small CLI that strips metadata (EXIF/XMP, PDF info, Office/OpenDocument properties) from files.

Supported: `jpg png webp tiff pdf docx xlsx pptx odt ods odp`

```
pip install -r requirements.txt
python metawipe.py photo.jpg report.pdf      # writes photo.clean.jpg, report.clean.pdf
python metawipe.py -r -i ~/Pictures          # overwrite originals, recursively
python metawipe.py -o out/ *.docx            # write cleaned copies into out/
```

Note: removes document properties, not content. Text inside the document, embedded images,
tracked changes and comments are left alone. Review sensitive files by hand.
