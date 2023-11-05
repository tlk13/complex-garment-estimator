train: 
	conda init bash
	source ~/.bashrc
	conda activate garments
	export PYTHONPATH=$PYTHONPATH:/local/home/tkesdogan/Desktop/garments/extra/Garment-Pattern-Generator/packages
	python nn/train.py -c ./models/att/att.yaml


install: 
	conda install -c conda-forge svgwrite
	conda install -c conda-forge svglib
	pip install sparsemax
	pip install entmax
	
