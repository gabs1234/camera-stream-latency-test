"""Offline source-log join: raw values remain intact in a new CSV."""
import csv

from .core import summary


def report(args):
    source = {}
    if args.source_log:
        with open(args.source_log, newline='') as file:
            for row in csv.DictReader(file):
                if row['event'] == 'presented':
                    key = row['session'], row['sequence']
                    if key in source:
                        raise ValueError('duplicate source presentation key')
                    source[key] = row
    extra = ['source_submit_wall_ns', 'source_schedule_error_ms', 'source_flags',
             'submit_referenced_arrival_ms', 'join_status']
    values, corrected = [], []
    with open(args.measurements, newline='') as file:
        reader = csv.DictReader(file)
        fields = reader.fieldnames
        if not fields or 'arrival_latency_ms' not in fields:
            raise ValueError('not a receiver CSV')
        output = open(args.output, 'x', newline='') if args.output else None
        try:
            writer = csv.DictWriter(output, fields + extra) if output else None
            if writer:
                writer.writeheader()
            for row in reader:
                first = row.get('first_observation') == '1'
                if first:
                    values.append(float(row['arrival_latency_ms']))
                match = source.get((row.get('session'), row.get('sequence')))
                row['join_status'] = 'no_source_match'
                if match:
                    if row['marker_ns'] != match['marker_ns']:
                        row['join_status'] = 'timestamp_conflict'
                    else:
                        row.update(source_submit_wall_ns=match['submit_wall_ns'],
                                   source_schedule_error_ms=match['schedule_error_ms'],
                                   source_flags=match['flags'], join_status='matched')
                        value = (int(row['arrival_wall_ns'])-int(match['submit_wall_ns']))/1e6
                        row['submit_referenced_arrival_ms'] = value
                        if first:
                            corrected.append(value)
                if writer:
                    writer.writerow(row)
        finally:
            if output:
                output.close()
    print('First-observed arrival latency (all): ' + summary(values))
    if args.source_log:
        print('Matched first observations, referenced to source submission (all): ' + summary(corrected))
        print('Submission is a software timestamp; physical display timing remains unknown.')
