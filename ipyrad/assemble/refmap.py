#!/usr/bin/env python2.7

""" 
Aligns reads to a reference genome. Returns unaligned reads to the de novo
pipeline. Aligned reads get concatenated to the *clustS.gz files at the 
end of step3
"""

from __future__ import print_function

import os
import gzip
import glob
import shutil
import ipyrad
import numpy as np
import subprocess as sps
from util import *
from ipyrad.assemble.rawedit import comp

import logging
LOGGER = logging.getLogger(__name__)

# pylint: disable=W0142
# pylint: disable=W0212
# pylint: disable=R0915
# pylint: disable=R0914
# pylint: disable=R0912
# pylint: disable=C0301



def index_reference_sequence(data, force=False):
    """ 
    Index the reference sequence, let smalt bail out if it already exists.
    """

    ## get ref file from params
    refseq_file = data.paramsdict['reference_sequence']

    # These are smalt specific index files. We don't ever reference them 
    # directly except here to make sure they exist, so we don't need to keep em
    index_sma = refseq_file+".sma"
    index_smi = refseq_file+".smi"
    # samtools specific index
    index_fai = refseq_file+".fai"

    if all([os.path.isfile(i) for i in [index_sma, index_smi, index_fai]]):
        if force:
            print("    Force reindexing of reference sequence")
        else:
            print("    Reference sequence index exists")
            return

    msg = """\
    *************************************************************
    Indexing reference sequence. 
    This only needs to be done once, and takes just a few minutes
    ************************************************************* """
    if data._headers:
        print(msg)

    ## Create smalt index for mapping
    ## smalt index [-k <wordlen>] [-s <stepsiz>]  <index_name> <reference_file>
    cmd1 = [ipyrad.bins.smalt, "index", 
            "-k", str(data._hackersonly["smalt_index_wordlen"]), 
            refseq_file, 
            refseq_file]

    ## call the command
    proc1 = sps.Popen(cmd1, stderr=sps.STDOUT, stdout=sps.PIPE)
    error1 = proc1.communicate()[0]

    ## simple samtools index for grabbing ref seqs
    cmd2 = [ipyrad.bins.samtools, "faidx", refseq_file]
    proc2 = sps.Popen(cmd2, stderr=sps.STDOUT, stdout=sps.PIPE)

    ## call the command:
    error2 = proc2.communicate()[0]

    ## error handling
    if proc1.returncode:
        raise IPyradWarningExit(error1)
    if proc2.returncode:
        if "please use bgzip" in error2:
            raise IPyradWarningExit(NO_ZIP_BINS.format(refseq_file))
        else:
            raise IPyradWarningExit(error2)

    ## print finished message
    if data._headers:
        print("    Done indexing reference sequence")



def mapreads(data, sample, nthreads):
    """ 
    Attempt to map reads to reference sequence. This reads in the fasta files
    (samples.files.edits), and maps each read to the reference. Unmapped reads 
    are dropped right back in the de novo pipeline. Reads that map successfully
    are processed and pushed downstream and joined with the rest of the data 
    post muscle_align. 

    Mapped reads end up in a sam file.
    Unmapped reads are pulled out and put in place of the edits (.fasta) file. 

    The raw edits .fasta file is moved to .<sample>.fasta to hide it in case 
    the mapping screws up and we need to roll-back. Mapped reads stay in the 
    sam file and are pulled out of the pileup later.
    """

    LOGGER.info("Entering mapreads(): %s %s", sample.files.edits, nthreads)

    ## This is the input derep file, for paired data we need to split the data, 
    ## and so we will make sample.files.dereps == [derep1, derep2], but for 
    ## SE data we can simply use sample.files.derep == [derepfile].
    derepfile = os.path.join(data.dirs.edits, sample.name+"_derep.fastq")
    sample.files.dereps = [derepfile]

    ## This is the final output files containing merged/concat derep'd refmap'd 
    ## reads that did not match to the reference. They will be back in 
    ## merge/concat (--nnnnn--) format ready to be input to vsearch, if needed. 
    mumapfile = sample.files.unmapped_reads
    umap1file = os.path.join(data.dirs.edits, sample.name+"-tmp-umap1.fastq")
    umap2file = os.path.join(data.dirs.edits, sample.name+"-tmp-umap2.fastq")        

    ## These are persistent file handles that were already set in setup_dirs()
    ## sample.files.unmapped_reads = os.path.join(data.dirs.edits, 
    ##                               sample.name+"-refmap_derep.fastq")mumapfile
    ## sample.files.mapped_reads = os.path.join(data.dirs.refmapping, 
    ##                                          sample.name+"-mapped-sorted.bam")

    ## split the derepfile into the two handles we designate
    if "pair" in data.paramsdict["datatype"]:
        sample.files.split1 = os.path.join(data.dirs.edits, sample.name+"-split1.fastq")
        sample.files.split2 = os.path.join(data.dirs.edits, sample.name+"-split2.fastq")
        sample.files.dereps = [sample.files.split1, sample.files.split2]
        split_merged_reads(sample.files.dereps, derepfile)

    ## (cmd1) smalt <task> [TASK_OPTIONS] [<index_name> <file_name_A> [<file_name_B>]]
    ##  -f sam       : Output as sam format, tried :clip: to hard mask output 
    ##                 but it shreds the unmapped reads (outputs empty fq)
    ##  -l [pe,mp,pp]: If paired end select the orientation of each read
    ##  -n #         : Number of threads to use
    ##  -x           : Perform a more exhaustive search
    ##  -y #         : proportion matched to reference (sequence similarity)
    ##  -o           : output file
    ##               : Reference sequence
    ##               : Input file(s), in a list. One for R1 and one for R2
    ##  -c #         : proportion of the query read length that must be covered

    ## (cmd2) samtools view [options] <in.bam>|<in.sam>|<in.cram> [region ...] 
    ##   -b = write to .bam
    ##   -F = Select all reads that DON'T have this flag. 
    ##         0x4 (segment unmapped)
    ##   -U = Write out all reads that don't pass the -F filter 
    ##        (all unmapped reads go to this file).

    ## (cmd3) samtools sort [options...] [in.bam]
    ##   -T = Temporary file name, this is required by samtools, ignore it
    ##        Here we hack it to be samhandle.tmp cuz samtools cleans it up
    ##   -O = Output file format, in this case bam
    ##   -o = Output file name

    ## The output SAM data is written to file (-o)
    ## input is either (derep) or (derep-split1, derep-split2)
    cmd1 = [ipyrad.bins.smalt, "map", 
            "-f", "sam", 
            "-n", str(max(1, nthreads)),
            "-y", str(data.paramsdict['clust_threshold']), 
            "-o", os.path.join(data.dirs.refmapping, sample.name+".sam"),
            "-x",
            data.paramsdict['reference_sequence']
            ] + sample.files.dereps

    ## Reads in the SAM file from cmd1. It writes the unmapped data to file
    ## and it pipes the mapped data to be used in cmd3
    cmd2 = [ipyrad.bins.samtools, "view", 
           "-b", 
           "-F", "0x4", 
           "-U", os.path.join(data.dirs.refmapping, sample.name+"-unmapped.bam"), 
           os.path.join(data.dirs.refmapping, sample.name+".sam")]

    ## this is gonna catch mapped bam output from cmd2 and write to file
    cmd3 = [ipyrad.bins.samtools, "sort", 
            "-T", os.path.join(data.dirs.refmapping, sample.name+".sam.tmp"),
            "-O", "bam", 
            "-o", sample.files.mapped_reads]

    ## this is gonna read the sorted BAM file and index it. only for pileup?
    cmd4 = [ipyrad.bins.samtools, "index", sample.files.mapped_reads]

    ## this is gonna read in the unmapped files, args are added below, 
    ## and it will output fastq formatted unmapped reads for merging.
    cmd5 = [ipyrad.bins.samtools, "bam2fq", 
            os.path.join(data.dirs.refmapping, sample.name+"-unmapped.bam")]

    ## Insert additional arguments for paired data to the commands.
    ## We assume Illumina paired end reads for the orientation 
    ## of mate pairs (orientation: ---> <----). 
    if 'pair' in data.paramsdict["datatype"]:
        ## add paired flag (-l pe) to cmd1 right after (smalt map ...) 
        cmd1.insert(2, "pe")
        cmd1.insert(2, "-l")
        ## add samtools filter for only keep if both pairs hit
        cmd2.insert(2, "0x2")
        cmd2.insert(2, "-f")
        ## tell bam2fq that there are output files for each read pair
        cmd5.insert(2, umap1file)
        cmd5.insert(2, "-1")
        cmd5.insert(2, umap2file)
        cmd5.insert(2, "-2")
    else:
        cmd5.insert(2, mumapfile)
        cmd5.insert(2, "-0")

    ## Running cmd1 creates ref_mapping/sname.sam, 
    LOGGER.debug(" ".join(cmd1))
    proc1 = sps.Popen(cmd1, stderr=sps.STDOUT, stdout=sps.PIPE)

    ## This is really long running job so we wrap it to ensure it dies. 
    try:
        error1 = proc1.communicate()[0]
    except KeyboardInterrupt:
        proc1.kill()

    ## raise error if one occurred in smalt
    if proc1.returncode:
        raise IPyradWarningExit(error1)

    ## Running cmd2 writes to ref_mapping/sname.unmapped.bam, and 
    ## fills the pipe with mapped BAM data
    LOGGER.debug(" ".join(cmd2))
    proc2 = sps.Popen(cmd2, stderr=sps.STDOUT, stdout=sps.PIPE)

    ## Running cmd3 pulls mapped BAM from pipe and writes to 
    ## ref_mapping/sname.mapped-sorted.bam. 
    ## Because proc2 pipes to proc3 we just communicate this to run both.
    LOGGER.debug(" ".join(cmd3))
    proc3 = sps.Popen(cmd3, stderr=sps.STDOUT, stdout=sps.PIPE, stdin=proc2.stdout)
    error3 = proc3.communicate()[0]
    if proc3.returncode:
        raise IPyradWarningExit(error3)
    proc2.stdout.close()

    ## Later we're gonna use samtools to grab out regions using 'view', and to
    ## do that we need it to be indexed. Let's index it now. 
    LOGGER.debug(" ".join(cmd4))
    proc4 = sps.Popen(cmd4, stderr=sps.STDOUT, stdout=sps.PIPE)
    error4 = proc4.communicate()[0]
    if proc4.returncode:
        raise IPyradWarningExit(error4)
    
    ## Running cmd5 writes to either edits/sname-refmap_derep.fastq for SE
    ## or it makes edits/sname-tmp-umap{12}.fastq for paired data, which 
    ## will then need to be merged.
    LOGGER.debug(" ".join(cmd5))
    proc5 = sps.Popen(cmd5, stderr=sps.STDOUT, stdout=sps.PIPE)
    error5 = proc5.communicate()[0]
    if proc5.returncode:
        raise IPyradWarningExit(error5)

    ## Finally, merge the unmapped reads, which is what cluster()
    ## expects. If SE, just rename the outfile. In the end
    ## <sample>-refmap_derep.fq will be the final output
    if 'pair' in data.paramsdict["datatype"]:
        LOGGER.info("Merging unmapped reads {} {}".format(umap1file, umap2file))
        ## merge_pairs wants the files to merge in this stupid format,
        ## also the first '1' at the end means revcomp R2, and the
        ## second 1 means "really merge" don't just join w/ nnnn
        merge_pairs(data, [(umap1file, umap2file)], mumapfile, 1, 1)



def ref_muscle_chunker(data, sample):
    """ 
    Run bedtools to get all overlapping regions. Pass this list into the func
    'get_overlapping_reads' which will write fastq chunks to the clust.gz file.

    1) Run bedtools merge to get a list of all contiguous blocks of bases
    in the reference seqeunce where one or more of our reads overlap.
    The output will look like this:
            1       45230754        45230783
            1       74956568        74956596
            ...
            1       116202035       116202060
    """
    LOGGER.info('entering ref_muscle_chunker')

    ## Get regions, which will be a giant list of 5-tuples, of which we're 
    ## only really interested in the first three: (chrom, start, end) position.
    regions = bedtools_merge(data, sample)

    if len(regions) > 0:
        get_overlapping_reads(data, sample, regions)

    else:
        msg = "No reads mapped to reference sequence - {}".format(sample.name)
        LOGGER.warn(msg)



def get_overlapping_reads(data, sample, regions):
    """
    For SE data, this pulls mapped reads out of sorted mapped bam files and 
    appends them to the clust.gz file so they fall into downstream 
    (muscle alignment) analysis. 

    For PE data, this pulls mapped reads out of sorted mapped bam files, splits 
    R1s from R2s and writes them to separate files. Once all reads are written, 
    it calls merge_reads (vsearch) to find merged and non-merged reads. These
    are then put into clust.gz with either an nnnn separator or as merged. 
    
    The main func being called here is 'bam_region_to_fasta', which calls
    samtools to pull out the mapped reads. 

    1) Coming into this function we have sample.files.mapped_reads 
        as a sorted bam file, and a passed in list of regions to evaluate.
    2) Get all reads overlapping with each individual region.
    3) Pipe to vsearch for clustering.
    4) Append to the clust.gz file.
    """

    ## storage and counter
    locus_list = []
    reads_merged = 0

    ## Set the write mode for opening clusters file.
    ## 1) if "reference" then only keep refmapped, so use 'wb' to overwrite 
    ## 2) if "denovo+reference" then 'ab' adds to end of denovo clust file
    write_flag = 'wb'
    if data.paramsdict["assembly_method"] == "denovo+reference":
        write_flag = 'ab'

    ## file handle for writing clusters
    sample.files.clusters = os.path.join(data.dirs.clusts, sample.name+".clust.gz")
    outfile = gzip.open(sample.files.clusters, write_flag)

    ## write a separator if appending to clust.gz
    if data.paramsdict["assembly_method"] == "denovo+reference":
        outfile.write("\n//\n//\n")

    # Wrap this in a try so we can easily locate errors
    try:
        ## For each identified region, build the pileup and write out the fasta
        for line in regions.strip().split("\n"):

            # Blank lines returned from bedtools screw things up. Filter them.
            if line == "":
                continue

            ## get elements from bedtools region
            chrom, region_start, region_end = line.strip().split()[0:3]

            ## bam_region_to_fasta returns a chunk of fasta sequence
            args = [data, sample, chrom, region_start, region_end]
            clust = bam_region_to_fasta(*args)

            ## If bam_region_to_fasta fails for some reason it'll return [], 
            ## in which case skip the rest of this. Normally happens if reads
            ## map successfully, but too far apart.
            if not clust:
                continue

            ## Store locus in a list
            #LOGGER.info("clust from bam-region-to-fasta \n %s", clust)
            locus_list.append(clust)

            ## write chunk of 1000 loci and clear list to minimize memory
            if not len(locus_list) % 1000:
                outfile.write("\n//\n//\n".join(locus_list)+"\n//\n//\n")
                locus_list = []
        
        ## write remaining
        if any(locus_list):
            outfile.write("\n//\n//\n".join(locus_list))
        else:
            ## If it's empty, strip off the last \n//\n//\n from the outfile.
            pass

        ## close handle
        outfile.close()

    except Exception as inst:
        LOGGER.error("Exception inside get_overlapping_reads - {}".format(inst))
        raise

    finally:
        LOGGER.info("Total merged reads for {} - {}"\
                     .format(sample.name, reads_merged))
        sample.stats.reads_merged = reads_merged



def split_merged_reads(outhandles, input_derep):
    """
    Takes merged/concat derep file from vsearch derep and split it back into 
    separate R1 and R2 parts. 
    - sample_fastq: a list of the two file paths to write out to.
    - input_reads: the path to the input merged reads
    """

    handle1, handle2 = outhandles
    splitderep1 = open(handle1, 'w')
    splitderep2 = open(handle2, 'w')

    with open(input_derep, 'r') as infile: 
        ## Read in the infile two lines at a time: (seqname, sequence)
        duo = itertools.izip(*[iter(infile)]*2)

        ## lists for storing results until ready to write
        split1s = []
        split2s = []

        ## iterate over input splitting, saving, and writing.
        idx = 0
        while 1:
            try:
                itera = duo.next()
            except StopIteration:
                break
            ## split the duo into separate parts and inc counter
            part1, part2 = itera[1].split("nnnn")
            idx += 1

            ## R1 needs a newline, but R2 inherits it from the original file            
            ## store parts in lists until ready to write
            split1s.append("{}{}\n".format(itera[0], part1))
            split2s.append("{}{}".format(itera[0], part2))

            ## if large enough then write to file
            if not idx % 10000:
                splitderep1.write("".join(split1s))
                splitderep2.write("".join(split2s))
                split1s = []
                split2s = [] 

    ## write final chunk if there is any
    if any(split1s):
        splitderep1.write("".join(split1s))
        splitderep2.write("".join(split2s))

    ## close handles
    splitderep1.close()
    splitderep2.close()



def check_insert_size(data, sample):
    """
    check mean insert size for this sample and update 
    hackersonly.max_inner_mate_distance if need be. This value controls how 
    far apart mate pairs can be to still be considered for bedtools merging 
    downstream.
    """

    ## pipe stats output to grep
    cmd1 = [ipyrad.bins.samtools, "stats", sample.files.mapped_reads]
    cmd2 = ["grep", "SN"]
    proc1 = sps.Popen(cmd1, stderr=sps.STDOUT, stdout=sps.PIPE)
    proc2 = sps.Popen(cmd2, stderr=sps.STDOUT, stdout=sps.PIPE, stdin=proc1.stdout)

    ## get piped result
    res = proc2.communicate()[0]

    ## raise exception on failure and do cleanup
    if proc2.returncode:
        raise IPyradWarningExit("error in %s: %s", cmd2, res)
        
    ## starting vals
    avg_insert = 0
    stdv_insert = 0
    avg_len = 0

    ## iterate over results
    for line in res.split("\n"):
        if "insert size average" in line:
            avg_insert = float(line.split(":")[-1].strip())

        elif "insert size standard deviation" in line:
            ## hack to fix sim data when stdv is 0.0. Shouldn't
            ## impact real data bcz stdv gets rounded up below
            stdv_insert = float(line.split(":")[-1].strip()) + 0.1
       
        elif "average length" in line:
            avg_len = float(line.split(":")[-1].strip())

    LOGGER.debug("avg {} stdv {} avg_len {}"\
                 .format(avg_insert, stdv_insert, avg_len))

    ## If all values return successfully set the max inner mate distance.
    ## This is tricky. avg_insert is the average length of R1+R2+inner mate
    ## distance. avg_len is the average length of a read. If there are lots
    ## of reads that overlap then avg_insert will be close to but bigger than
    ## avg_len. We are looking for the right value for `bedtools merge -d`
    ## which wants to know the max distance between reads. 
    if all([avg_insert, stdv_insert, avg_len]):
        ## If 2 * the average length of a read is less than the average
        ## insert size then most reads DO NOT overlap
        if stdv_insert < 5:
            stdv_insert = 5.
        if (2 * avg_len) < avg_insert:
            hack = avg_insert + (3 * np.math.ceil(stdv_insert)) - (2 * avg_len)

        ## If it is > than the average insert size then most reads DO
        ## overlap, so we have to calculate inner mate distance a little 
        ## differently.
        else:
            hack = (avg_insert - avg_len) * (3 * np.math.ceil(stdv_insert))
            

        ## set the hackerdict value
        LOGGER.info("stdv: hacked insert size is %s", hack)
        data._hackersonly["max_inner_mate_distance"] = int(np.math.ceil(hack))

    else:
        ## If something fsck then set a relatively conservative distance
        data._hackersonly["max_inner_mate_distance"] = 300
        LOGGER.debug("inner mate distance for {} - {}".format(sample.name,\
                    data._hackersonly["max_inner_mate_distance"]))



def bedtools_merge(data, sample):
    """ 
    Get all contiguous genomic regions with one or more overlapping
    reads. This is the shell command we'll eventually run

    bedtools bamtobed -i 1A_0.sorted.bam | bedtools merge [-d 100]
        -i <input_bam>  :   specifies the input file to bed'ize
        -d <int>        :   For PE set max distance between reads
    """
    LOGGER.info("Entering bedtools_merge: %s", sample.name)
    mappedreads = os.path.join(data.dirs.refmapping, 
                               sample.name+"-mapped-sorted.bam")

    ## command to call `bedtools bamtobed`, and pipe output to stdout
    ## Usage:   bedtools bamtobed [OPTIONS] -i <bam> 
    ## Usage:   bedtools merge [OPTIONS] -i <bam> 
    cmd1 = [ipyrad.bins.bedtools, "bamtobed", "-i", mappedreads]
    cmd2 = [ipyrad.bins.bedtools, "merge", "-i", "-"]

    ## Set the -d flag to tell bedtools how far apart to allow mate pairs.
    if 'pair' in data.paramsdict["datatype"]:
        check_insert_size(data, sample)
        #cmd2.insert(2, str(data._hackersonly["max_inner_mate_distance"]))
        cmd2.insert(2, str(data._hackersonly["max_inner_mate_distance"]))
        cmd2.insert(2, "-d")

    ## pipe output from bamtobed into merge
    LOGGER.info("stdv: bedtools merge cmds: %s %s", cmd1, cmd2)
    proc1 = sps.Popen(cmd1, stderr=sps.STDOUT, stdout=sps.PIPE)
    proc2 = sps.Popen(cmd2, stderr=sps.STDOUT, stdout=sps.PIPE, stdin=proc1.stdout)
    result = proc2.communicate()[0]
    proc1.stdout.close()

    ## check for errors and do cleanup
    if proc2.returncode:
        raise IPyradWarningExit("error in %s: %s", cmd2, result)

    ## Report the number of regions we're returning
    nregions = len(result.strip().split("\n"))
    LOGGER.info("bedtools_merge: Got # regions: %s", nregions)
    return result



def bam_region_to_fasta(data, sample, chrom, region_start, region_end):
    """ 
    Take the chromosome position, and start and end bases and return sequences
    of all reads that overlap these sites. This is the command we're building:

    samtools view -b 1A_sorted.bam 1:116202035-116202060 | \
             samtools bam2fq <options> -

            -b      : output bam format
            -0      : For SE, output all reads to this file
            -1/-2   : For PE, output first and second reads to different files
            -       : Tell samtools to read in from the pipe

    Write out the sam output and parse it to return as fasta for clust.gz file. 
    We also grab the reference sequence with a @REF header to aid in alignment
    for single-end data. This will be removed post-alignment. 
    """

    ## output bam file handle for storing genome regions
    bamf = sample.files.mapped_reads
    if not os.path.exists(bamf):
        raise IPyradWarningExit("  file not found - %s", bamf)

    ## a string argument as input to commands, indexed at either 0 or 1, 
    ## and with pipe characters removed from chromo names
    rstring_id1 = "{}:{}-{}"\
        .format(chrom.replace("|", "_"), str(int(region_start)+1), region_end)
    rstring_id0 = "{}:{}-{}"\
        .format(chrom.replace("|", "_"), str(region_start), region_end)

    ## The "samtools faidx" command will grab this region from reference 
    ## which we'll paste in at the top of each stack to aid alignment.
    cmd1 = [ipyrad.bins.samtools, "faidx", 
            data.paramsdict["reference_sequence"], 
            rstring_id1]

    ## Call the command, I found that it doesn't work with shell=False if 
    ## the refstring is 'MT':100-200', but it works if it is MT:100-200. 
    LOGGER.info("Grabbing bam_region_to_fasta:\n %s", cmd1)
    proc1 = sps.Popen(cmd1, stderr=sps.STDOUT, stdout=sps.PIPE)
    ref = proc1.communicate()[0]
    if proc1.returncode:
        raise IPyradWarningExit("  error in %s: %s", cmd1, ref)

    ## parse sam to fasta. Save ref location to name.
    name, seq = ref.strip().split("\n", 1)
    seq = "".join(seq.split("\n"))
    fasta = ["{}_REF;+\n{}".format(name, seq)]

    ## if PE then you have to merge the reads here
    if "pair" in data.paramsdict["datatype"]:
        ## TODO: Can we include the reference sequence in the PE clust.gz?
        ## if it's longer than the merged pairs it makes hella indels
        ## Drop the reference sequence for now...
        fasta = []

        ## Create temporary files for R1, R2 and merged, which we will pass to
        ## the function merge_pairs() which calls vsearch to test merging.
        prefix = os.path.join(data.dirs.refmapping, 
                        "{}-{}".format(sample.name, rstring_id0))
        read1 = "{}-R1".format(prefix)
        read2 = "{}-R2".format(prefix)
        merged = "{}-merged".format(prefix)

        ## command to do the 'view' in samtools
        cmd1 = [ipyrad.bins.samtools, "view", "-b", bamf, rstring_id0]
        cmd2 = [ipyrad.bins.samtools, "bam2fq", "-1", read1, "-2", read2, "-"]

        ## run commands, pipe 1 -> 2, then cleanup
        proc1 = sps.Popen(cmd1, stderr=sps.STDOUT, stdout=sps.PIPE)
        proc2 = sps.Popen(cmd2, stderr=sps.STDOUT, stdout=sps.PIPE, stdin=proc1.stdout)
        res = proc2.communicate()[0]
        if proc2.returncode:
            raise IPyradWarningExit("error {}: {}".format(cmd2, res))
        proc1.stdout.close()

        ## merge the pairs. 0 means don't revcomp bcz samtools already
        ## did it for us. 1 means "actually merge".
        try:
            ## return number of merged reads, writes merged data to 'merged'
            ## we don't yet do anything with the returned number of merged 
            _ = merge_pairs(data, [(read1, read2)], merged, 0, 1)
            with open(merged, 'r') as infile:
                quatro = itertools.izip(*[iter(infile)]*4)
                while 1:
                    try:
                        bits = quatro.next()
                    except StopIteration:
                        break
                    ## TODO: figure out a real way to get orientation for PE
                    orient = "+"
                    fullfast = ">{a};{b};{c};{d}\n{e}".format(
                        a=bits[0].split(";")[0],
                        b=rstring_id1,
                        c=bits[0].split(";")[1], 
                        d=orient,
                        e=bits[1].strip())
                    #,e=bits[9])
                    fasta.append(fullfast)
            ## clean up
            os.remove(merged)
            os.remove(read1)
            os.remove(read2)

        except Exception as inst:
            ## Failed merging, probably unequal number of reads in R1 and R2
            ## Skip this locus?
            LOGGER.debug("Failed to merge reads, continuing; %s", inst)

       
    else:
        ## SE if faster than PE cuz it skips writing intermedidate files
        cmd2 = [ipyrad.bins.samtools, "view", bamf, rstring_id0]
        proc2 = sps.Popen(cmd2, stderr=sps.STDOUT, stdout=sps.PIPE)

        ## run and check outputs
        res = proc2.communicate()[0]
        if proc2.returncode:
            raise IPyradWarningExit("{} {}".format(cmd2, res))

        ## do not join seqs that 
        for line in res.strip().split("\n"):
            bits = line.split("\t")

            ## Read in the 2nd field (FLAGS), convert to binary
            ## and test if the 7th bit is set which indicates revcomp
            orient = "+"
            if int('{0:012b}'.format(int(bits[1]))[7]):
                orient = "-"

            ## Rip insert the mapping position between the seq label and
            ## the vsearch derep size.
            fullfast = ">{a};{b};{c};{d}\n{e}".format(
                a=bits[0].split(";")[0],
                b=rstring_id0,
                c=bits[0].split(";")[1], 
                d=orient, 
                e=bits[9])
            fasta.append(fullfast)

    return "\n".join(fasta)



def refmap_stats(data, sample):
    """ 
    Get the number of mapped and unmapped reads for a sample
    and update sample.stats 
    """
    ## shorter names
    mapf = os.path.join(data.dirs.refmapping, sample.name+"-mapped-sorted.bam")
    umapf = os.path.join(data.dirs.refmapping, sample.name+"-unmapped.bam")

    ## get from unmapped
    cmd1 = [ipyrad.bins.samtools, "flagstat", umapf]
    proc1 = sps.Popen(cmd1, stderr=sps.STDOUT, stdout=sps.PIPE)
    result1 = proc1.communicate()[0]

    ## get from mapped
    cmd2 = [ipyrad.bins.samtools, "flagstat", mapf]
    proc2 = sps.Popen(cmd2, stderr=sps.STDOUT, stdout=sps.PIPE)
    result2 = proc2.communicate()[0]

    ## store results
    sample.stats["refseq_unmapped_reads"] = int(result1.split()[0])
    sample.stats["refseq_mapped_reads"] = int(result2.split()[0])



def refmap_init(data, sample):
    """ create some file handles for refmapping """
    ## make some persistent file handles for the refmap reads files
    sample.files.unmapped_reads = os.path.join(data.dirs.edits, 
                                  "{}-refmap_derep.fastq".format(sample.name))
    sample.files.mapped_reads = os.path.join(data.dirs.refmapping,
                                  "{}-mapped-sorted.bam".format(sample.name))



## GLOBALS
NO_ZIP_BINS = """
    Reference sequence must be uncompressed fasta or bgzip compressed,
    your file is probably gzip compressed. The simplest fix is to gunzip
    your reference sequence by running this command:

        gunzip {}

    Then edit your params file to remove the `.gz` from the end of the
    path to your reference sequence file and rerun step 3 with the `-f` flag.
    """



if __name__ == "__main__":
    from ipyrad.core.assembly import Assembly

    shutil.copy("/tmp/wat", "/tmp/watt")
    ## test...
    DATA = Assembly("test")
    DATA.get_params()
    DATA.set_params(1, "./")
    DATA.set_params(28, '/Volumes/WorkDrive/ipyrad/refhacking/MusChr1.fa')
    DATA.get_params()
    #DATA.step3()
    #PARAMS = {}
    #FASTQS = []
    #QUIET = 0
    #run(PARAMS, FASTQS, QUIET)
