
#!/usr/bin/env python2.7

""" define ipyrad analysis class object. 
    This is used for the following:
        -- to store and modify a params dictionary.
        -- to view analysis history graph.
        -- to load data that is saved to disk.
        -- to run ipyrad assembly steps on samples linked to it. """

from __future__ import print_function
import time
import glob
import os
import sys
import subprocess
import pandas as pd
import dill
from collections import OrderedDict
from ipyrad.core.sample import Sample
from ipyrad import assemble

# pylint: disable=E1101
# pylint: disable=E1103


## TODO: consens files need their own directory, 
## NESTED DATA STRUCTURE
## best way to make analyses bifurcating
## e.g., clust.95/cons_d6j6/

## cite herrera about hard to estimate restriction sites
## cite Seetharam & Stuart as another predicted study from one genome
## cite Peterson et al. (2012) for thinking they can estimate Nloci

## allow combinatorial indexing

class Assembly(object):
    """ An ipyrad analysis object """
    def __init__(self, name=""):
        ## a project name
        self.name = name

        ## get binaries of dependencies
        self.vsearch, self.muscle = getbins()

        ## link a log history of executed workflow
        self.log = []
        self.stamp(self.name+" created")
        self.statsfiles = {}

        ## samples linked 
        self.samples = {}

        ## multiplex files linked
        self.barcodes = {}
        self.rawdata = []

        ## the default params dict
        self.paramsdict = OrderedDict([
                       ("working_directory", os.path.curdir),
                       ("raw_fastq_path", os.path.join(
                                                 os.path.curdir,
                                                 "*.fastq")),
                       ("barcodes_path", os.path.join(
                                                os.path.curdir,
                                                "*.barcodes.txt")),
                       ("sorted_fastq_path", ""),
                       ("restriction_overhang", ("TGCAG", "")),
                       ("max_low_qual_bases", 5),
                       ("N_processors", 4),
                       ("mindepth_statistical", 6), 
                       ("mindepth_majrule", 6), 
                       ("datatype", 'rad'), 
                       ("clust_threshold", .85),
                       ("minsamp", 4), 
                       ("max_shared_heterozygosity", .25), 
                       ("prefix_outname", self.name),
                       ("phred_Qscore_offset", 33),
                       ("max_barcode_mismatch", 1),
                       ("filter_adapters", 0), 
                       ("filter_min_trim_len", 35), 
                       ("ploidy", 2), 
                       ("max_stack_size", 1000),
                       ("max_Ns_consens", 5), 
                       ("max_Hs_consens", 8), 
                       ("max_SNPs_locus", (100, 100)), 
                       ("max_Indels_locus", (5, 99)), 
                       ("trim_overhang", (1, 2, 2, 1)), 
                       ("hierarchical_clustering", 0)
        ])
    
        ## Require user to link Sample objects 
        ## self.link_barcodes()
        ## link barcodes dict if already in barcodes_path
        #if os.path.exists(self.paramsdict["barcodes_path"]):
        #    self.barcodes = self.link_barcodes()

    @property
    def stats(self):
        """ returns a data frame with sample data and state """
        nameordered = self.samples.keys()
        nameordered.sort()
        return pd.DataFrame([self.samples[i].stats for i in nameordered], 
                      index=nameordered)

    # @property
    # def files(self):
    #     """ returns a data frame with sample data and state """
    #     nameordered = self.samples.keys()
    #     nameordered.sort()
    #     return pd.DataFrame([self.samples[i].statsfiles for i in nameordered], 
    #                   index=nameordered)



    def __getstate__(self):
        return self.__dict__

    def __setstate__(self, dicto):
        self.__dict__.update(dicto)


    def stamp(self, event):
        """ stamp an event into the log history """
        tev = time.strftime("%m/%d/%y %H:%M:%S", time.gmtime())
        self.log.append((self.name, tev, event))


    def link_raws(self):
        """ link raw data files """
        ## TODO: remove from link_fastqs
        if os.path.isdir(self.paramsdict["raw_fastq_path"]):
            self.paramsdict["raw_fastq_path"] += "*"
        raws = glob.glob(os.path.join(
                            self.paramsdict["raw_fastq_path"],
                            ))
        if raws:
            for rawfile in raws:
                self.rawdata.append(rawfile)


    def link_fastqs(self, pear=0):
        """ Create Sample objects for samples in sorted_fastq_path. """
        ## does location exist, if nothing selected, select all
        if os.path.isdir(self.paramsdict["sorted_fastq_path"]):
            self.paramsdict["sorted_fastq_path"] += "*"
        if os.path.isdir(self.paramsdict["raw_fastq_path"]):
            self.paramsdict["raw_fastq_path"] += "*"

        ## grab fastqs/fq/gzip/all
        fastqs = glob.glob(os.path.join(
                            self.paramsdict["sorted_fastq_path"],
                            ))
        raws = glob.glob(os.path.join(
                            self.paramsdict["raw_fastq_path"],
                            ))

        ## create a Sample object for each fastq file
        if fastqs:
            ## link pairs into tuples
            if 'pair' in self.paramsdict["datatype"]:
                if "_R1_" in any([i for i in fastqs]):
                    r1_files = [i for i in fastqs if "_R1_" in i]
                    fastqs = [(i, i.replace("_R1_", "_R2_")) for i in r1_files]
                else:
                    r1_files = [i for i in fastqs if "_R1." in i]
                    fastqs = [(i, i.replace("_R1.", "_R2.")) for i in r1_files]
            else:
                fastqs = [(i, ) for i in fastqs]

            for fastq in fastqs:
                ## remove file extension from name
                sname = name_from_file(fastq[0])

                ## create Sample
                samp = Sample(sname)
                samp.stats["state"] = 1
                samp.barcode = "pre_demultiplexed"
                samp.files['fastq'] = fastq

                ## check if data were pear_merged
                if pear:
                    samp.pear = 1
                else:
                    if '.forward' in fastq[0]:
                        print("warning: if data are merged with PEAR "+\
                              "enter link_fastqs(pear=1)")
                self.samples[samp.name] = samp
        if raws:
            for rawfile in raws:
                self.rawdata.append(rawfile)

        ## TODO: if fastqs already sorted, try to link stats
  

    def link_edits(self, sample=""):
        """ link existing fasta files from the edits/ directory in the
        working directory to an Assembly object. Used to restart an 
        analysis from step3, or to link files for extracting stats.
        Sample names can be entered to select individual samples from 
        edits/ otherwise all are attempted to be linked. If there is 
        alread a Sample in Assembly.samples with the same name, the 
        edits files are linked to that Sample """
        if sample:
            ## link a single sample
            pass
        else:
            ## do all samples in expected location ($wd/edits/)
            editdir = os.path.join(
                self.paramsdict["working_directory"], "edits")
            for fname in glob.glob(os.path.join(editdir, "*")):
                ## get sample name from file name
                sname = name_from_file(fname)
                ## check that Sapmle does not already exist
                if sname in self.samples:
                    ## enter location
                    self.samples[sname].files["edits"] = fname
                    if self.samples[sname].stats['state'] < 3:
                        ## sample has not completed clustering
                        self.samples[sname].stats['state'] = 2
                    ## try to link stats file...
                else:
                    ## not in samples, make new
                    sample = Sample(sname)
                    sample.stats['state'] = 2
                    sample.files["filtered"] = fname
                    self.samples[sample.name] = sample
            ## if Sample not in Assembly, (re)make it.
        ## try linking stats from stats file, 
        ## else try linking stats from samples



    def link_barcodes(self):
        """ creates a self.barcodes object to save barcodes info 
            as a dictionary, if there is a barcodes file in 
            self.paramsdict["barcodes_path"] """

        ## in case fuzzy selected
        try: 
            barcodefile = glob.glob(self.paramsdict["barcodes_path"])[0]
        except IndexError: 
            print("Barcodes file not found:", self.paramsdict["barcodes_path"])

        #if not os.path.exists(barcodefile):
        #    print("Barcodes file not found:", self.paramsdict["barcodes_path"])
        #else:            
        bdf = pd.read_csv(barcodefile, header=None, 
                                       delim_whitespace=1)
        bdf = bdf.dropna()
        ## make sure upper case
        bdf[1] = bdf[1].str.upper()
        ## set to Assembly object
        self.barcodes = dict(zip(bdf[0], bdf[1]))

            # ## for each barcode create a Sample
            # for key in self.barcodes:
            #     samp = Sample(key)
            #     samp.state = 0
            #     samp.barcode = self.barcodes[key]
            #     if samp not in self.samples:
            #         self.samples[samp.name] = samp


    #def link_sample(self, sample):
    #    """ attempts to link a sample to the Assembly object. 
    #    If the sample does not have a name conflict it can be linked. 
    #    Can take a single sample object or a list of sample objects"""
    #    pass


    def get_params(self, param=""):
        """ pretty prints params if called as a function """
        fullcurdir = os.path.realpath(os.path.curdir)
        if not param:
            for index, (key, value) in enumerate(self.paramsdict.items()):
                if isinstance(value, str):
                    value = value.replace(fullcurdir, ".")
                sys.stdout.write("  {:<4}{:<30}{:<45}\n".format(index+1,
                           key, value))
        else:
            try:
                if int(param):
                    #sys.stdout.write(self.paramsdict.values()[int(param)-1])
                    return self.paramsdict.values()[int(param)-1]
            except (ValueError, TypeError, NameError, IndexError):
                return 'key not recognized'


        #def save(self, name=""):
        #    if not name:
        #        print("must enter a filename for saved object")
        #    else:
        #        json.dumps(self)


    def set_params(self, param, newvalue):
        """ Set parameter to different value. Raises error if param 
        is wrong type or in conflict"""
        ## TODO: change structure to use assertions?

        ## make string
        param = str(param)

        ## if matching
        if param in ['1', 'working_directory']:
            self.paramsdict['working_directory'] = expander(newvalue)
            self.stamp("[1] set to "+newvalue)


        elif param in ['2', 'raw_fastq_path']:
            fullrawpath = expander(newvalue)
            if os.path.isdir(fullrawpath):
                fullrawpath = os.path.join(fullrawpath, "*.gz")
            self.paramsdict['raw_fastq_path'] = fullrawpath
            self.stamp("[2] set to "+newvalue)


        elif param in ['3', 'barcodes_path']:
            fullbarpath = expander(newvalue)
            if glob.glob(fullbarpath):
                self.paramsdict['barcodes_path'] = fullbarpath
                self.link_barcodes()
                self.stamp("[3] set to "+newvalue)
            elif not fullbarpath:
                self.paramsdict['barcodes_path'] = fullbarpath                
                self.stamp("[3] set to empty")
            else:
                print('cannot find barcodes file')


        elif param in ['4', 'sorted_fastq_path']:
            newvalue = expander(newvalue)
            if os.path.isdir(newvalue):
                newvalue = os.path.join(newvalue, "*.gz")
            self.paramsdict['sorted_fastq_path'] = newvalue
            self.link_fastqs()
            self.stamp("[4] set to "+newvalue)


        elif param in ['5', 'restriction_overhang']:
            if not isinstance(newvalue, tuple):
                print("must enter as a tuple, e.g., '(TGCAG, "")' )")
            else:
                self.paramsdict['restriction_overhang'] = newvalue
            self.stamp("[5] set to "+str(newvalue))


        elif param in ['6', 'max_low_qual_bases']:
            self.paramsdict['max_low_qual_bases'] = int(newvalue)
            self.stamp("[6] set to "+str(newvalue))


        elif param in ['7', 'N_processors']:
            self.paramsdict['N_processors'] = int(newvalue)
            self.stamp("[7] set to "+str(newvalue))


        elif param in ['8', 'mindepth_statistical']:
            ## do not allow values below 5
            if int(newvalue) < 5:
                print("error: mindepth statistical cannot be set < 5")
            ## do not allow majrule to be > statistical
            elif int(newvalue) < self.paramsdict["mindepth_majrule"]:
                print("error: mindepth statistical cannot be less than \
                       mindepth_majrule")                
            else:
                self.paramsdict['mindepth_statistical'] = int(newvalue)
                self.stamp("[8] set to "+str(newvalue))


        elif param in ['9', 'mindepth_majrule']:
            if int(newvalue) > self.paramsdict["mindepth_statistical"]:
                print("error: mindepth_majrule cannot be > \
                       mindepth_statistical")
            else:
                self.paramsdict['mindepth_majrule'] = int(newvalue)
                self.stamp("[9] set to "+str(newvalue))


        elif param in ['10', 'datatype']:
            ## list of allowed datatypes
            datatypes = ['rad', 'gbs', 'ddrad', 'pairddrad',
                         'pairgbs', 'merged', '2brad']
            ## raise error if something else
            if self.paramsdict['datatype'] not in datatypes:
                print("error: datatype not recognized")
            else:
                self.paramsdict['datatype'] = str(newvalue)
                self.stamp("[10] set to "+newvalue)


        elif param in ['11', 'clust_threshold']:
            self.paramsdict['clust_threshold'] = float(newvalue)
            self.stamp("[11] set to {}".format(newvalue))


        elif param in ['12', 'minsamp']:
            self.paramsdict['minsamp'] = int(newvalue)
            self.stamp("[12] set to {}".format(int(newvalue)))


        elif param in ['13', 'max_shared_heterozygosity']:
            self.paramsdict['max_shared_heterozygosity'] = newvalue
            self.stamp("[13] set to {}".format(newvalue))


        elif param in ['14', 'prefix_outname']:
            self.paramsdict['prefix_outname'] = newvalue
            self.stamp("[14] set to {}".format(newvalue))


        elif param in ['15', 'phred_Qscore_offset']:
            self.paramsdict['phred_Qscore_offset'] = int(newvalue)
            self.stamp("[15] set to {}".format(int(newvalue)))


        elif param in ['16', 'max_barcode_mismatch']:
            self.paramsdict['max_barcode_mismatch'] = int(newvalue)
            self.stamp("[16] set to {}".format(int(newvalue)))

        ### ....
        elif param in ['17', 'filter_adapters']:
            self.paramsdict['filter_adapters'] = int(newvalue)
            self.stamp("[17] set to "+str(newvalue))


        elif param in ['18', 'filter_min_trim_len']:
            self.paramsdict['filter_min_trim_len'] = int(newvalue)
            self.stamp("[18] set to {}".format(int(newvalue)))


        elif param in ['19', 'ploidy']:
            self.paramsdict['ploidy'] = int(newvalue)
            self.stamp("[19] set to {}".format(int(newvalue)))


        elif param in ['20', 'max_stack_size']:
            self.paramsdict['max_stack_size'] = int(newvalue)
            self.stamp("[20] set to {}".format(int(newvalue)))


        elif param in ['21', 'max_Ns_consens']:
            self.paramsdict['max_Ns_consens'] = int(newvalue)
            self.stamp("[21] set to {}".format(int(newvalue)))


        elif param in ['22', 'max_Hs_consens']:
            self.paramsdict['max_Hs_consens'] = int(newvalue)
            self.stamp("[22] set to {}".format(int(newvalue)))


        elif param in ['23', 'max_Hs_consens']:
            self.paramsdict['max_Hs_consens'] = int(newvalue)
            self.stamp("[22] set to {}".format(int(newvalue)))


        elif param in ['24', 'max_Indels_locus']:
            self.paramsdict['max_Indels_locus'] = int(newvalue)
            self.stamp("[24] set to {}".format(int(newvalue)))


        elif param in ['25', 'trim_overhang']:
            self.paramsdict['trim_overhang'] = int(newvalue)
            self.stamp("[25] set to {}".format(int(newvalue)))


        elif param in ['26', 'hierarchical_clustering']:
            self.paramsdict['hierarchical_clustering'] = int(newvalue)
            self.stamp("[26] set to {}".format(int(newvalue)))





    def file_tree(self):
        """ prints the project data structure """
        startpath = self.paramsdict["working_directory"]
        if startpath in [".", "", "./"]:
            print("./")
        else:
            for root, _, files in os.walk(startpath):
                level = root.replace(startpath, '').count(os.sep)
                indent = ' ' * 4 * (level)
                print('{}{}/'.format(indent, os.path.basename(root)))
                subindent = ' ' * 4 * (level + 1)
                for fname in files:
                    print('{}{}'.format(subindent, fname))


    def _save(self):
        """ pickle the data object """
        dillout = open(os.path.join(self.paramsdict["working_directory"],
                  self.name+".dataobj"), "wb")
        dill.dump(self, dillout)
        dillout.close()



    ### assembly methods that take or link Sample objects
    def step1(self, preview=0):
        """ step 1: demultiplex raw reads. """
        if not self.samples:
            assemble.demultiplex.run(self, preview)
            self.stamp("s1_demultiplexing:")
        else:
            print("samples already found in ", self.name, 
                  "use ip.merge to combine samples")
            # if append:
            #     assemble.demultiplex.run(self, preview)
            #     self.stamp("s1_demultiplexing [append]:")

        ## pickle the data obj
        self._save()


    def step2(self, sample="", preview=0):
        """ step 2: edit raw reads. Takes dictionary keys (sample names)
        either individually, or as a list, or it takes no argument to 
        select all samples in the Assembly object. Only samples in state
        =1 will be edited. If state > 2, file will be skipped. If you want
        to overwrite data for a file, first set its state to 1:
        data.samples['sample'].stats['state'] = 1 """

        if sample:
            ## if sample key, replace with sample obj
            if isinstance(sample, str):
                ## in case name doesn't match key
                skey = sample.replace("_R1_", "")
                if skey in self.samples:
                    sample = self.samples[skey]
                    assemble.rawedit.run(self, sample, preview)
                else:
                    print("sample", sample, "not in", self.name)
            else:
                if isinstance(sample, list):
                    for samp in sample:
                        ## get sample from dict key
                        samp = self.samples[samp]
                        assemble.rawedit.run(self, samp, preview)

        else:
            for _, sample in self.samples.items():
                assemble.rawedit.run(self, sample, preview)

        ## pickle the data obj
        self._save()


    def step3(self, samples=None, preview=0, noreverse=0, nthreads=4):
        """ step 3: clustering within samples """

        ## sampling
        if samples:
            ## if string make a list(tuple)
            if isinstance(samples, str):
                ## make sure pair names aren't used
                skey = samples.replace("_R1_", "")
                samples = [skey]

            ## make into a tuple list with (key, sample)
            ## filters out bad names
            subsamples = []
            for sample in samples:
                if self.samples.get(sample):
                    subsamples.append((sample, self.samples[sample]))
            if subsamples:
                ## if sample is a key, replace with sample obj
                print("clustering {} samples on {} processors".\
                      format(len(samples), self.paramsdict["N_processors"]))
                assemble.cluster_within.run(self, subsamples, preview, 
                                            noreverse, nthreads)
            else:
                print("No samples found. Check that names are correct")
        else:
            ## if no samples selected and no samples exist
            if not self.samples:
                ## try linking edits from working dir
                print("linked fastas from [working_directory]/edits")
                self.link_edits()
            ## run clustering for all samples
            print("clustering {} samples on {} processors".\
                  format(len(self.samples), self.paramsdict["N_processors"]))
            assemble.cluster_within.run(self, self.samples.items(),
                                        preview, noreverse, nthreads)

        ## pickle the data object
        self._save()



    def step4(self, samples="", preview=0):
        """ step 4: Joint estimation of error rate and heterozygosity. 
        If you want to overwrite data for a file, first set its state to 3:
        data.samples['sample'].stats['state'] = 3 """

        ## sampling
        if samples:
            ## make a list keys or samples
            if isinstance(samples, str):
                samples = list([samples])
            else:
                samples = list(samples)

            ## if keys are in list
            if any([isinstance(i, str) for i in samples]):
                ## make into a subsampled sample dict
                subsamples = {i: self.samples[i] for i in samples}

            ## send to function
            assemble.jointestimate.run(self, subsamples.values())
        else:
            ## if no sample, then do all samples
            if not self.samples:
                ## if no samples in data, try linking edits from working dir
                #self.link_clustfiles()
                if not self.samples:
                    print("Assembly object has no samples in state=3")
            ## run clustering for all samples
            assemble.jointestimate.run(self, self.samples.values())

        ## pickle the data object
        self._save()


    def run(self, steps=0):
        """ Select steps of an analysis. If no steps are entered then all
        steps are run. Enter steps as a string, e.g., "1", "123", "12345" """
        if not steps:
            steps = "123457"
        if '1' in steps:
            self.step1()
        if '2' in steps:
            self.step2()            
        if '3' in steps:
            self.step3()            
        # if '4' in steps:
        #     self.step4()            
        # if '5' in steps:
        #     self.step5()            
        # if '6' in steps:
        #     self.step6()            
        # if '7' in steps:
        #     self.step7()            





def name_from_file(fname):
    """ internal func: get the sample name from any pyrad file """
    file_extensions = [".gz", ".fastq", ".fq", ".fasta", 
                       ".clustS", ".consens"]
    base, ext = os.path.splitext(os.path.basename(fname))
    ## remove read number from name
    base = base.replace("_R1_", "")\
               .replace("_R1.", ".")
    ## remove extensions
    while ext in file_extensions:
        base, ext = os.path.splitext(base)
    return base


def expander(namepath):
    """ expand ./ ~ and ../ designators in location names """        
    if "~" in namepath:
        namepath = namepath.replace("~", os.path.expanduser("~"))
    if "../" in namepath:
        _, post = namepath.split("../")
        namepath = os.path.abspath(
                    os.path.join(
                        os.path.dirname(""), '..', post))
    elif "./" in namepath:
        _, post = namepath.split("./")
        namepath = os.path.abspath("")+"/"+post
    return namepath


def cmd_exists(cmd):
    """ check if dependency program is there """
    return subprocess.call("type " + cmd,
                           shell=True, 
                           stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE) == 0


def getbins():
    """ gets the right version of vsearch and muscle
    depending on linux vs osx """
    ## get platform mac or linux
    _platform = sys.platform

    ## get current location
    path = os.path.abspath(os.path.dirname(__file__))

    ## fin bin directory
    ipyrad_path = os.path.dirname(os.path.dirname(path))
    bin_path = os.path.join(ipyrad_path, "bin")

    ## get the correct binaries 
    if 'linux' in _platform:
        vsearch = os.path.join(
                       os.path.abspath(bin_path),
                       "vsearch-1.1.3-linux-x86_64")
        muscle = os.path.join(
                       os.path.abspath(bin_path),
                       "muscle3.8.31_i86linux64")
    else:
        vsearch = os.path.join(
                       os.path.abspath(bin_path),
                       "vsearch-1.1.3-osx-86_64")
        muscle = os.path.join(
                       os.path.abspath(bin_path),
                       "muscle3.8.31_i86darwin64")
    ## TODO: return error if system is 32-bit arch.
    return vsearch, muscle



if __name__ == "__main__":
    ## test...
    DATA = Assembly("test")
    DATA.get_params()
    DATA.set_params(1, "./")